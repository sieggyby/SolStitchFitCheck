"""Keep the contributor writing contract available without private home files.

Run with unittest to avoid importing the bot's private database dependency.
"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HEADING = "## Writing standard (applies to everything you write here)"

# Contract from feat/content-deck-phase0, d4b0fc6. Whitespace is not significant.
EXPECTED_RULES = """
- **Answer first.** The finding, the verdict, or the number goes in line one. No preamble, no
  restating the task, no announcing what you are about to do.
- **An answer is not a deliverable.** A review comment states its point once and stops; no closing recap. A document
  you were asked to produce runs as long as the work needs, and must carry **every element the
  request named**. Cutting a requested section is an omission, not brevity.
- **Brevity governs the output, never the analysis.** Think as long as the problem needs.
- Sentences: **20 words max when instructing, 25 when describing.** One idea per sentence.
  Active voice. One term per concept; never vary a term for elegance.
- **Put the condition or goal before the step.** "To X, do Y." "If A, do B."
- **Link text names its destination.** Never "here" or "this document".
- **Never use an em-dash or en-dash.** Write `--`, a comma, or a period.
- **Never hedge inline.** State the claim flat, then give the evidence and its limit in the next
  sentence. Not "this is probably a leak"; instead "This leaks. I traced one path. I did not
  check the retry branch."
- **Removing a hedge is not free.** If you cut "probably", you must add the limit sentence.
  Never write *cannot*, *always*, *never*, or *rules out* about something you have not verified.
  Never assert a mechanism you have not checked. "I did not test this" is a complete sentence.
- Plain word over the impressive one. Gloss an unavoidable term in five words or fewer.
- **Report faithfully.** If a test failed, say so with the output. If you skipped a step, say
  that. Do not describe a partial fix as complete.

This standard never overrides a finding. Accuracy first, then this.
"""


class WritingStandardTests(unittest.TestCase):
    def test_contributor_rules_are_self_contained_in_both_guides(self):
        for filename in ("AGENTS.md", "CLAUDE.md"):
            with self.subTest(guide=filename):
                text = (ROOT / filename).read_text(encoding="utf-8")
                self.assertEqual(text.count(HEADING), 1, f"{filename}: missing writing standard")
                section = text.split(HEADING, 1)[1].split("\n---", 1)[0]
                normalized = " ".join(section.split())
                self.assertIn(
                    "They govern your review prose, your commit messages, your `TODO(codex)` "
                    "notes, and any doc or comment you touch.",
                    normalized,
                )
                self.assertIn(" ".join(EXPECTED_RULES.split()), normalized)


if __name__ == "__main__":
    unittest.main()
