Make a document file for the owner; it is sent to him in this chat after your answer. This is the one thing you can
make: use it when he asks for a document, a file, minutes, a report, a letter, a table or a spreadsheet, and for the
minutes of a meeting recording he sent. `format`: `docx` (Word, the default for documents), `pdf`, `xlsx` (a
spreadsheet), `md` or `txt`. `filename`: a short name without an extension, in the owner's language. `title`: optional.
`content`: the whole document in Markdown (`#` headings, paragraphs, `-` or `1.` lists, `**bold**`, `*italic*`, pipe
tables), in the owner's language unless he asked otherwise. For `xlsx`, give `sheets` (`[{"name": …, "rows": [[header,
…], [cell, …]]}]`, numbers as numbers), or Markdown tables in `content`. The result says `made` and the file name, or
why it failed (`tell_owner`, when present, is what to tell him); mention the file in one short line of your answer and
do not repeat its content in full.
