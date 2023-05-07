import numpy as np
from bs4 import BeautifulSoup
import cssutils as csu
from copy import copy
from argparse import ArgumentParser
import re
import urllib
import os
from pathlib import Path
from collections import namedtuple
import unicodedata
from html import unescape

####
# parse google doc of guidelines AND editorial policies (html download) and reformat
# for OJS site with (nested?) dropdowns
#
# TODO:
# catch for any hdr2 sections that *start* with <ol> or <ul> (ln 344, 'prev' etc)
# re-combine oddly segmented nested lists? Might be more trouble than it's worth
# nested accordions? at least for one spot in policies for data availability/types
# and/or other things that are in paragraph-sections (could also reformat as ol post-google)

# goal: something that looks like the following
#
#   <div id="accordionExample" class="accordion">
#   <div class="card">
#   <div id="headingOne" class="card-header">
#   <h2 class="mb-0"><button class="btn btn-lg btn-light btn-block collapsed" type="button" data-toggle="collapse" data-target="#collapseOne" aria-expanded="false" aria-controls="collapseOne"> <span class="pull-left"> heading here </span> </button></h2>
#   </div>
#   <div id="collapseOne" class="collapse" aria-labelledby="headingOne" data-parent="#accordionExample">
#   <div class="card-body">text here</div>
#   </div>
#   </div>
#   <div class="card">
#   <div id="headingTwo" class="card-header">
#   <h2 class="mb-0"><button class="btn btn-lg btn-light btn-block collapsed" type="button" data-toggle="collapse" data-target="#collapseTwo" aria-expanded="false" aria-controls="collapseTwo"> <span class="pull-left"> heading here </span> </button></h2>
#   </div>
#   <div id="collapseTwo" class="collapse" aria-labelledby="headingTwo" data-parent="#accordionExample">
#   <div class="card-body">text here</div>
#   </div>
#   </div>
#   </div>
#
####

def strip_comments(soup,cmt_class):
    """
    decompose divs and <a>s from comments
    """
    for div in soup.find_all('div',class_=cmt_class):
        div.decompose()
    for a in soup.find_all(id=re.compile('^cmnt')):
        a.decompose()
    return soup

def find_comment_class(soup):
    """
    figure out what the css tag is for divs around comments so we can strip all of them out
    """
    divs = soup.find_all('div')
    for d in divs:
        aas = d.find_all(id=re.compile('^cmnt'))
        if len(aas) > 0:
            return d.attrs['class']  # assume all comments are the same class (seems safe)

def clean_soup(soup,h6=True,notext=True,aempty=True,sup=True):
    """
    clean up various kinds of empty tags that tend to show up in these files
    h6 and sup are mainly in the guidelines; notext and aempty are in both, probably?
    """
    if h6:
        for h in soup.find_all('h6'):
            h.unwrap()

    ingredients = soup.body.find_all(recursive=False)
    if notext:
        for ing in ingredients:
            if ing.text == None or ing.text == '':
                ing.decompose()
    if aempty:
        for a in soup.find_all('a'):
            if a.string == None:
                a.decompose()
    if sup:
        for s in soup.find_all('sup'):
            if s.string == None:
                s.decompose()
    return soup

def clean_spans(soup,translate={}):
    """
    get rid of empty span elements (mostly in guidelines) and translate any classes that 
    we can figure out (also mostly in guidelines)
    """
    for sp in soup.find_all('span'):
        for k in translate.keys():
            if 'class' in sp.attrs and k in sp.attrs['class']:
                for a in translate[k]:
                    sp.wrap(copy(a))
        sp.unwrap()
    return soup


def get_h1_h2(ingredients):
    """
    from a soup, extract indices of elements that contain h1 or h2 tags
    """
    hdr1 = []; hdr2 = []
    hdr1_text = []; hdr2_text = []
    for i,ing in enumerate(ingredients):
        if bool(ing.h1) or ing.name == 'h1':
            hdr1.append(i)
            gettext = ing.text.lower()
            gettext = re.sub(r'[^\w\s]','',gettext)  # strip out punctuation
            hdr1_text.append(gettext.replace(' ','-'))  # (messes with acc)
        elif bool(ing.h2):
            hdr2.append(i)
            gettext = ing.text.lower()
            gettext = re.sub(r'[^\w\s]','',gettext)
            hdr2_text.append(gettext.replace(' ','-'))
    hdr1 = np.array(hdr1); hdr2 = np.array(hdr2)
    return hdr1, hdr2, hdr1_text, hdr2_text

def class_translate(sheet,css_keys,match='.c'):
    """
    scan a stylesheet for (.c#) rules and pick out a particular set of css keys
    return dict of (.c#) keys and html tags that they should get, based on input css_keys dict
    """
    translate = {}
    for rule in sheet:  # loop all rules
        try:
            if rule.selectorText.startswith(match):  # find rules matching name criterion
                ruledict = {}
                for k in css_keys.keys():  # find style tags that match css_keys top level
                    if k in rule.style.keys():
                        ruledict[k] = rule.style[k]
                rulelist = []
                if ruledict != {}:
                    for k in ruledict.keys():  # check whether tag contents need to be translated
                        if ruledict[k] in css_keys[k].keys():
                            rulelist.append(css_keys[k][ruledict[k]])
                if len(rulelist) > 0:
                    # if tag needs translation, translate it
                    # NOTE we strip leading . from the rule name
                    translate[rule.selectorText.split('.')[-1]] = rulelist
        except AttributeError:
            pass  # no selectorText, probably the link at the top
    return translate

def _ol_info(idivtext):
    """
    get start numbers and #items in each ol in a chunk of stuff
    """
    ols = idivtext.find_all('ol',recursive=False)
    if len(ols) > 1:
        lis = np.zeros(len(ols),dtype=int); sts = np.zeros(len(ols),dtype=int)
        for io,ol in enumerate(ols):
            # count li elements at level 1
            lis[io] = len(ol.find_all('li',recursive=False))
            # get start #s
            sts[io] = int(ol.attrs['start'])
    return ols, sts, lis

def check_whose(idivtext):
    """
    test whether any bits of <ol> set are un-nested
    """
    ols = idivtext.find_all('ol',recursive=False)
    # check if we need to recursively nest any ols
    if len(ols) > 1:
        ol_list,sts,lis = _ol_info(idivtext)
        # see if sts and li are consistent with each other
        whose = np.cumsum(lis)[:-1] + 1 == sts[1:] 
        return np.all(whose)
    else:
        return True  # only one <ol>, everything is fine (or had better be)


def nest_in_between(idivtext):
    """
    nest extra bits (<p> etc) that fell between chunks of <ol>
    """
    ols = idivtext.find_all('ol',recursive=False)
    # check if we need to recursively nest any ols
    if len(ols) > 1:
        ol_list,sts,lis = _ol_info(idivtext)
        # see if sts and li are consistent with each other
        whose = np.cumsum(lis)[:-1] + 1 == sts[1:]
        for io in range(len(ol_list)-1):
            if ol_list[io].next_sibling.name != 'ol':  # something to append
                for g in ol_list[io].next_siblings:
                    if g == ol_list[io+1]:
                        break
                    else:
                        toadd = g.extract()
                        ol_list[io].append(toadd)

    return idivtext

def nest_lis(idivtext):
    """
    put <p> and similar elements that fall in <ol> but not <li> in <li>
    doesn't actually work at the moment (31 May 2022)
    or maybe sort of works but would need to be run iteratively or something?
    basically google docs does not understand lists with multiple paragraphs per <li>
    """
    ols = idivtext.find_all('ol',recursive=False)

    for ol in ols:
        lis = ol.find_all('li',recursive=False)
        ing = ol.find_all(recursive=False)
        if len(lis) != len(ing):  # there are elements that are not in list items
            if len(lis) > 1:
                for il in range(len(lis)-1):
                    if lis[il].next_sibling.name != 'li':
                        for g in lis[il].next_siblings:
                            if g == lis[il+1]:
                                break
                            else:
                                toadd = g.extract()
                                lis[il].append(toadd)
            else:
                if lis[0].next_sibling.name != 'li':
                    for g in lis[0].next_siblings:
                        toadd = g.extract()
                        lis[0].append(toadd)
    return idivtext

# here are some css tags that we want to translate, and how we want to translate them
# NOTE <u> is maybe not best practice? Also here I think it only applies to hyperlinks.
css_keys = {
    "font-weight": {"700":"strong"},
    "font-style": {"italic": "em"},
    "text-decoration": {"underline": "u"},
    "background-color": {"#ff0": "mark"},
}

def _has_href(tag):
    """
    function to pass to find_all() to get all hyperlinks
    """
    return tag.has_attr('href')

if __name__ == '__main__':

    tmp_dir = os.path.join(Path(__file__).resolve().parent, "tmp")
    out_dir = os.path.join(Path(__file__).resolve().parent, "policy_documents")

    # start by exporting google doc as html and extracting the html file from the zip archive
    # (we don't need any image files afaik)

    # filename and type (guidelines or not, ie editorial policies) can be set by command line args
    # if those aren't present, we ask for the info via input()
    parser = ArgumentParser()
    parser.add_argument("--ifile", "-f", metavar="ifile", type=str, help="path to input file")
    args = parser.parse_args()

    ifile = args.ifile
    # ifile = "policies.html"  # for debugging only
    if ifile == None:
        ifile = input("Enter path to input file: ") or "policies.html"
    ifile = os.path.join(tmp_dir, ifile)
    assert os.path.isfile(ifile), f"file `{ifile}` does not exist"

    # Get input HTML
    with open(ifile, "r") as f:
        text = f.readline() # google docs outputs html as one single line, weirdly

    soup = BeautifulSoup(text, "html.parser")  # parse to a soup
    
    """ Header parsing """

    header = soup.head.extract()
    if bool(soup.img): soup.img.decompose()  # get rid of the header image (seismica logo)
        # (only for guidelines, but doesn't hurt ed pol b/c there are no images in it)

    # deal with css style in header, to some extent
    style = csu.parseString(header.style.text)  # parses to CSSStyleSheet object
    # we will only look at .c# styles, and find italics, bold, and underline
    #   [info on what is looked for/translated is in css_keys before __main__]
    # we're skipping all the hyper-specific list element formatting at the moment
    translate_tags = class_translate(style, css_keys)
    translate = {}  # need to actually make soup tags to wrap things in; do this outside of function
    for k in translate_tags.keys():
        translate[k] = []
        for a in translate_tags[k]:
            translate[k].append(soup.new_tag(a))
    

    """ Intermediate cleaning """

    # figure out what the comment div class name is, strip out comments
    cmt_class = find_comment_class(soup)
    soup = strip_comments(soup, cmt_class=cmt_class)

    # clean up span formatting, translate to html tags since we can't use css header
    soup = clean_spans(soup, translate=translate)

    # clean out empty tags etc
    soup = clean_soup(soup)  # not all apply to ed pol, but that's actually fine

    # make a copy of the soup and empty it so we can add things back in
    bowl = copy(soup)
    bowl.body.clear()
    del bowl.body["class"]  # for neatness


    """ Accordion tag placeholders """

    # set up generic accordion tags that can be modified later
    span = bowl.new_tag("span")
    span.attrs["class"] = "pull-left"
    span.string = "heading here"

    button = bowl.new_tag("button")
    button.attrs = {
        "class": "btn btn-lg btn-light btn-block collapsed",
        "type": "button",
        "data-toggle": "collapse",
        "data-target": "#collapse01",
        "aria-expanded": "false",
        "aria-controls": "collapse01",
    }

    h2 = bowl.new_tag("h2")
    h2.attrs["class"] = "mb-0"

    divhead = bowl.new_tag("div")
    divhead.attrs = {
        "id": "heading01",
        "class": "card-header",
    }

    divcol = bowl.new_tag("div")
    divcol.attrs = {
        "id": "collapse01",
        "class": "collapse",
        "aria-labelledby": "heading01",
        "data-parent": "#accid",
    }

    divtext = bowl.new_tag("div")
    divtext.attrs["class"] = "card-body" 
    # divtext.string = "text here"

    card = bowl.new_tag("div")
    card.attrs["class"] = "card"

    A = namedtuple("accordion", ["span", "button", "h2", "divhead", "divcol", "divtext", "card"])
    accordion = A(span, button, h2, divhead, divcol, divtext, card)
    # The del line is just for debuggin. Remove at will
    del span, button, h2, divhead, divcol, divtext, card


    """ Body parsing """

    # go through body of soup element-wise, and deal with each in turn
    ingredients = soup.body.find_all(recursive=False)  # reset list
    # run through ingredients and map out where the headers and such are for overall structure
    h1, h2, h1text, h2text = get_h1_h2(ingredients)

    everything = {}  # dict for holding content so we can transfer duplicates
    # SPLIT HERE for ed pol vs guidelines in main loop
    for i, (this_h1, this_h1_text) in enumerate(zip(h1, h1text)):
        
        # Check if there is a next h1 tag
        if this_h1 != h1[-1]:
            next_h1 = h1[i+1]
        # If not: go to the end
        else:
            next_h1 = len(ingredients)

        # put in h1 header for marking
        new_h1 = bowl.new_tag("h1")
        new_h1.string = ingredients[this_h1].text
        bowl.body.append(new_h1)

        # start building the accordion
        acc_id = f"acc_{this_h1_text}"  # id from section head - long but at least not arbirtray
        accord = bowl.new_tag("div")
        accord.attrs = {
            "id": acc_id,
            "class": "accordion",
        }
        bowl.body.append(accord)  # we'll insert elements as they are made

        # go through the h2 markers, and between each, preserve whatever's there
        inds_h2 = (h2 > this_h1) & (h2 < next_h1)

        h2_use = h2[inds_h2]
        h2t_use = np.array(h2text)[inds_h2]

        for j, (this_h2, this_h2_text) in enumerate(zip(h2_use, h2t_use)):

            print(this_h1_text, this_h2_text)
            
            # Check if there is a next h2 tag
            if this_h2 != h2_use[-1]:
                next_h2 = h2_use[j+1]
            # If not: go to the end
            else:
                next_h2 = next_h1

            """
            The structure of an accordion card is as follows:

            <div>

                <div header>
                    <h2>
                        <button>
                            <span>Header test</span
                        </button>
                    </h2>
                </div header>

                <div body>
                    <div wrapper>
                        Content...
                    </div wrapper>
                </div body>

            </div
            """
            
            # Create a new card
            icard = copy(accordion.card)
            accord.append(icard)

            # Add a span with h2
            ispan = copy(accordion.span)
            ispan.string = ingredients[this_h2].text.strip()
            icard.insert(0, ispan)

            # Add a button with h2 text and wrap around span
            ibutton = copy(accordion.button)
            ibutton.attrs["data-target"] = f"#{this_h2_text}"
            ibutton.attrs["aria-controls"] = this_h2_text
            ispan.wrap(ibutton)

            # Wrap h2 tag around button
            ih2 = copy(accordion.h2)
            ibutton.wrap(ih2)

            # Wrap wrap div head around h2 tag
            idivhead = copy(accordion.divhead)
            idivhead.attrs["id"] = this_h2_text
            ih2.wrap(idivhead)

            # Create collapsible content div
            idivcol = copy(accordion.divcol)
            idivcol.attrs["id"] = this_h2_text
            idivcol.attrs["aria-labelledby"] = this_h2_text
            idivcol.attrs["data-parent"] = f"#{acc_id}"
            icard.insert(1, idivcol)

            # check if this content already exists in a previous accordion
            if (len(everything) > 0) and (this_h2_text in everything.keys()):
                """
                This block allows for recycling of content that is listed as
                "See [elsewhere]" in the Google Doc
                """
                print(f"Header `{this_h2_text}` found in a previous accordion (currently at `{this_h1_text}`). Copying contents...")
                idivtext = copy(everything[this_h2_text])
                idivcol.insert(0, idivtext)
            else:
                # Insert a new wrapper div
                idivtext = copy(accordion.divtext)
                idivcol.insert(0, idivtext)

                """ Loop over all elements up to next h2 """
                for k in range(this_h2 + 1, next_h2):

                    ing = ingredients[k]
                    
                    # Table handling
                    if ing.name == "table": # this should be the reviewer recommendations table - MvdE: and scope?
                        ing.attrs["class"] = "table"
                        if ing.thead is None:  # no header line, need to make the first row a header
                            first_row = ing.tr.extract()
                            thead = bowl.new_tag("thead")
                            ing.insert(0, thead)
                            thead.append(first_row)
                            for td in first_row.find_all("td"): 
                                td.wrap(bowl.new_tag("th")) 
                                td.unwrap() 
                        idivtext.append(ing)

                    # List handling
                    elif ing.name == "ul":  # put this back in the hierarchy with the previous ol
                        prev = ingredients[k-1]  # should be ol
                        if prev.name == "ol":
                            prev = idivtext.find_all("ol")[-1]
                            ul = ing.extract()
                            prev.append(ul)
                        else:
                            idivtext.append(ing)

                    # No handling
                    else:
                        idivtext.append(ing)

            
                # check <ol>s within this card; if the first one has start != 1, reset it
                # (this happens at one particular point in the reviewer guidelines at the moment)
                ols = idivtext.find_all("ol")
                if len(ols) > 0 and ols[0].attrs["start"] != 1:
                    ols[0].attrs["start"] = "1"

                # check if we need to recursively nest any ols
                if check_whose(idivtext):
                    idivtext = nest_in_between(idivtext)
                else:
                    # there's a mis-nested thing here; deal with it
                    iq = False
                    ol_list,sts,lis = _ol_info(idivtext)
                    whose = np.cumsum(lis)[:-1] + 1 == sts[1:]
                    while not iq:
                        olstart = ol_list[np.where(whose == False)[0][0]]  # this should not work??
                        iadd = True
                        while iadd:
                            toadd = olstart.next_sibling.extract()
                            if toadd.name == "ol":
                                iadd = False
                            olstart.append(toadd)
                        ol_list,sts,lis = _ol_info(idivtext)
                        whose = np.cumsum(lis)[:-1] + 1 == sts[1:]
                        if np.all(whose):
                            iq = True

            # nest extra bits (<p> etc) one more time now that numbers are matched
            idivtext = nest_in_between(idivtext)
            everything[this_h2_text] = idivtext  # save in case this is duplicated

        """ End loop over h2 elements """


    """ End loop over h1 elements """

    """ Parse URIs """

    # Pattern to match external URL
    url_needle = re.compile("https://www\.google\.com/url\?q=([^&]+)")

    # Find all URLs in the bowl
    links = bowl.find_all(_has_href)

    # Loop over URLs
    for link in links:
        # Check if pattern matches
        match = url_needle.search(urllib.parse.unquote(link.attrs["href"]))
        if match is not None:
            # If a match: extract only the second part (after /url?q=...)
            link.attrs["href"] = match.group(1)

    # border the tables
    for tab in bowl.find_all("table"):
        tab.attrs["style"] = "border:1px solid black;border-collapse:collapse"
    for th in bowl.find_all("th"):
        th.attrs["style"] = "border:1px solid black"
    for td in bowl.find_all("td"):
        td.attrs["style"] = "border:1px solid black"

    # <p> list items:
    for li in bowl.find_all("li"):
        li.name = "p"
        li.wrap(bowl.new_tag("li"))    

    # Smooth the soup
    bowl.smooth()

    for h1 in bowl.find_all("h1"):
        div = h1.find_next_sibling("div")

        second_bowl = BeautifulSoup()
        # second_bowl.append(h1)
        second_bowl.append(div)

        # Convert characters to unicode
        s = unescape(second_bowl.prettify())
        s = unicodedata.normalize("NFKC", s)

        """ Regex pattern fixing """

        # Reposition spaces that are inside <a> tags
        a_needle = re.compile(r"<a ([^>]*)>\s*(.*?)\s*<\/a>")
        s = a_needle.sub(r" <a \1>\2</a> ", s)

        # Reposition spaces that are inside other tags (with no attributes)
        for tag in ("em", "u", "strong"):
            needle = re.compile(rf"<{tag}>\s*(.*?)\s*<\/{tag}>")
            s = needle.sub(rf" <{tag}>\1</{tag}> ", s)

        # Replace the following occurrences:
        # <u><a>...</a></u
        ua_needle = re.compile(r"<u>[^<]*(<a [^>]*>[^<]*</a>)[^>]*<\/u>")
        # space(s) followed by punctuation
        space_needle = re.compile(r"\s*([,.;:\)\?])")
        # a ( followed by a space
        space_needle2 = re.compile(r"([\(])\s*")
        for needle in (ua_needle, space_needle, space_needle2):
            s = needle.sub(r"\1", s)

        # Fix commas not followed by a space, except if followed by a number
        space_needle3 = re.compile(r",(?!\s|\d)(?=[^,]*,)")
        s = space_needle3.sub(", ", s)

        # Replace </strong>[spaces]\n[spaces]<strong>
        strong_needle = re.compile(r"<\/strong>\s*\n\s*<strong>")
        s = strong_needle.sub("", s)

        # Fix unicode characters
        replace_dict = {
            "’": "'",
            "‘": "'",
            "“": "\"",
            "”": "\"",
            "–": "-",
            "—": "-",
        }
        for key, val in replace_dict.items():
            s = s.replace(key, val)

        """
        Generate output
        """

        fname = h1.text.lower()
        fname = re.sub(r"[^\w\s]", "", fname).replace(" ", "-")
        ofile = os.path.join(out_dir, f"{fname}.html")

        # Write to output file
        with open(ofile, "w") as f:
            f.write(s)

    print("Done")
