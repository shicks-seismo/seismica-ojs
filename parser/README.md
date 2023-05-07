# Guidelines parsing

The Guidelines for Authors, Reviewers and Editors, and the Editorial Policies, currently live in a single Google Doc. The script `parse_google_doc.py` is used to translate those documents into convenient html with collapsible headings for display on the OJS site. The steps to generate copy-paste ready html output is are the following:

1. Export the policies/guidelines Google Doc as an html file and save it to the `parser/tmp/` directory (`tmp` is not tracked by git)
2. Run the `parse_google_doc.py` script and enter the filename of the exported html file
3. Upon termination of the script, clean html files are written to `parser/policy_documents` (one file per category)
4. Copy and paste the contents of each file into the corresponding OJS pages (found under `Website > Setup > Navigation`). Make sure to work directly in the page html source (button "Source code"), and to only replace the code starting with `<div id="..." class="accordion">`

The `parser/policy_documents` is tracked by git, so changes to the html output can be tracked over time on GitHub.


### Dependencies
- [BeautifulSoup 4](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
- numpy
- [cssutils](https://cthedot.de/cssutils/)
