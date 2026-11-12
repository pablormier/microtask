from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import card_format
import card_references
import tasklib


class ReferenceTests(unittest.TestCase):
    def test_prose_boundaries_case_and_first_appearance(self):
        text = "😀 See (@ABC12345), @abc and @ABC12345. " \
               "email@abcdef.test @abcdefghi @abcdef123 @abcdef-more @@abc \\@def"
        self.assertEqual(card_references.extract_references(text), ["@ABC12345", "@abc"])
        spans = card_references.reference_spans(text)
        self.assertEqual([text[start:end] for start, end in spans],
                         ["@ABC12345", "@abc", "@ABC12345"])

    def test_code_links_html_and_urls_are_literal(self):
        text = """See @abc.
`@def` and ``code ` @fed``; unmatched ` then @123.
~~~python
@aaa
~~~~

    @bbb
    - @bbc is code, not a list
[label @ccc](https://example.org/path(with)/@ddd)
![image @eee](x.png)
[@fff][source] and [@aab][] and [@aac]
[source]: https://example.org/@aad
[@aab]: https://example.org
[@aac]: https://example.org
<!-- @aae --> <a href="/@aaf">@aba</a> <code>@abb</code>
https://example.org/@abc <https://example.org/@abd>
Afterwards @abe.
> ```
> @abf
> ```
- Parent @aca
    - Child @acb
      Continuation @acc
      ```
      @acd
      ```
"""
        self.assertEqual(card_references.extract_references(text), ["@abc", "@123", "@abe", "@aca", "@acb", "@acc"])

    def test_resolution_preserves_ambiguity_and_missing_targets(self):
        first = SimpleNamespace(kind="task", short_ref="abcdef12", title="First", frontmatter={"id": "first"})
        second = SimpleNamespace(kind="task", short_ref="abc12345", title="Second", frontmatter={"id": "second"})
        index = tasklib.ReferenceIndex([first, second])
        self.assertEqual(index.resolve("@ABC")['state'], "ambiguous")
        self.assertEqual(index.resolve("@fff")['state'], "missing")
        resolved = index.resolve("@ABCD")
        self.assertEqual(resolved['state'], "resolved")
        self.assertEqual(resolved['target'], {"id": "first", "ref": "@abcdef12", "title": "First", "kind": "task"})

    def test_body_and_comment_references_ignore_code(self):
        body = "See @abcdef12.\n\n```\n@fff\n```\n\n## Notes\nRelated @fed.\n"
        properties = {"id": "capture", "kind": "note", "title": "Capture", "priority": "none",
                      "created":"2026-11-10", "updated":"2026-11-10",
                      "comments": [{"at": "2026-11-10", "text": "Follow up @abc123."}]}
        raw = card_format.render_document(properties, body)
        header, _ = card_format.parse_frontmatter(raw)
        note = tasklib.Card(Path("cards/capture.md"), raw, header, body, properties)
        target = SimpleNamespace(kind="task", short_ref="abcdef12", title="Target", frontmatter={"id": "target"})
        references = tasklib.item_references(note, tasklib.ReferenceIndex([target, note]))
        self.assertEqual([ref['ref'] for ref in references], ["@abcdef12", "@fed", "@abc123"])
        self.assertEqual([ref['state'] for ref in references], ["resolved", "missing", "missing"])


if __name__ == "__main__":
    unittest.main()
