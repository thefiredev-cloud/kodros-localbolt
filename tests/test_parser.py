import unittest

from localbolt import llm

SAMPLE = """Sure, here is the app.
=== FILE: index.html ===
<!doctype html>
<h1>Hi</h1>
=== END FILE ===
=== FILE: js/app.js ===
```js
console.log("a");
```
=== END FILE ===
=== DELETE: old.css ===
SUMMARY: Built a greeting page.
"""


def run(text, size=None):
    p = llm.BlockParser()
    evs = []
    if size is None:
        evs += list(p.feed(text))
    else:
        for i in range(0, len(text), size):
            evs += list(p.feed(text[i:i + size]))
    evs += list(p.finish())
    return evs


class ParserTests(unittest.TestCase):
    def expect(self, evs):
        self.assertEqual(
            [e for e in evs if e[0] != "file_start"],
            [
                ("file", "index.html", "<!doctype html>\n<h1>Hi</h1>\n"),
                ("file", "js/app.js", 'console.log("a");\n'),
                ("delete", "old.css"),
                ("summary", "Built a greeting page."),
            ],
        )

    def test_whole(self):
        self.expect(run(SAMPLE))

    def test_every_chunk_size_gives_same_result(self):
        for size in (1, 2, 3, 5, 7, 16, 50):
            with self.subTest(size=size):
                self.expect(run(SAMPLE, size))

    def test_file_start_emitted_before_end(self):
        evs = run(SAMPLE)
        self.assertEqual(evs[0], ("file_start", "index.html"))

    def test_truncated_file_is_incomplete(self):
        evs = run("=== FILE: a.html ===\n<p>cut off")
        self.assertEqual(evs[-1], ("incomplete", "a.html"))
        self.assertFalse(any(e[0] == "file" for e in evs))

    def test_marker_text_inside_file_body_is_content(self):
        evs = run("=== FILE: a.md ===\nSUMMARY: not a summary\n=== DELETE: x.js ===\n=== END FILE ===\n")
        self.assertEqual([e for e in evs if e[0] == "file"], [("file", "a.md", "SUMMARY: not a summary\n=== DELETE: x.js ===\n")])
        self.assertFalse(any(e[0] in ("summary", "delete") for e in evs))

    def test_crlf_and_blank_padding(self):
        evs = run("=== FILE: a.txt ===\r\n\r\nhello\r\n\r\n=== END FILE ===\r\n")
        self.assertEqual([e for e in evs if e[0] == "file"], [("file", "a.txt", "hello\n")])

    def test_prose_only_yields_nothing(self):
        self.assertEqual(run("I cannot do that.\nSorry."), [])


class PromptTests(unittest.TestCase):
    def test_new_project_prompt_has_no_files_section(self):
        m = llm.build_messages("a clock", {})
        self.assertEqual(m[0]["role"], "system")
        self.assertIn("a clock", m[-1]["content"])
        self.assertNotIn("CURRENT FILE", m[-1]["content"])

    def test_edit_prompt_includes_files_and_budget(self):
        files = {"index.html": "<h1>x</h1>", "big.js": "y" * 500}
        m = llm.build_messages("make it red", files, budget=100)
        body = m[-1]["content"]
        self.assertIn("=== CURRENT FILE: index.html ===", body)
        self.assertNotIn("y" * 50, body)
        self.assertIn("big.js (500 chars)", body)

    def test_history_is_trimmed_and_mapped(self):
        hist = [{"role": "user", "text": f"u{i}"} for i in range(10)] + [{"role": "error", "text": "boom"}]
        m = llm.build_messages("go", {}, hist)
        self.assertEqual(len(m), 1 + 6 + 1 - 1)  # error dropped, last 6 of 11 minus the error
        self.assertNotIn("boom", str(m))


if __name__ == "__main__":
    unittest.main()
