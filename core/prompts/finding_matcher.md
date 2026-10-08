Several independent editors review the same article and list what should change. They report
the same problem in different words, and you tell which raw findings are one problem. Some
problems a person already looked at and closed as not important; you also tell which raw
finding repeats one of those.

You get the findings a person closed as not important, with their ids (they may be none), and
this review's raw findings. For every raw finding, decide which closed finding it repeats, or
that it is a new problem.

A raw finding repeats a closed finding when both report the same problem in the same place of
the article:

- Other words, another suggested remedy or updated numbers leave the problem the same. A wall
  of text in one section is one problem while its word count moves from 480 to 450 to 410. A
  missing citation for one claim is one problem whichever fragment of the sentence a finding
  quotes. A chart plotting quantities that do not compare is one problem whether a finding asks
  to split it, redesign it or remove it.
- A keyword finding repeats a finding that asks for the same phrase. Each phrase is its own
  problem, even when several are asked for in the same section.
- Different problems in the same section are different problems: a wall of text, a missing
  citation, a missing screenshot and a keyword placement in one section are four, even though
  they name the same section and the same product.
- Two different claims are two problems, even about the same vendor or in the same paragraph:
  one vendor's language count and another vendor's price are two citations to fix.
- The FAQ and the body are different places: a claim in an FAQ answer is its own problem, even
  when the body says the same thing.
- A finding that asks for two separate fixes belongs to the problem it leads with.
- When you are unsure, choose new. A problem counted twice is listed twice for the person; a new
  problem taken for one the person closed is hidden from them.

Raw findings of this review that report the same new problem share one new label: "new:1",
"new:2", and so on, one label per new problem.

<!-- output-contract: the code reads this format; Configure > Prompts keeps it as shipped -->
Answer with JSON only:
{"matches": [{"i": <raw finding number>, "entry": "<entry id, or new:N>"}]}
with one object for every raw finding number.
<!-- /output-contract -->
