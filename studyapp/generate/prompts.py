"""The system prompt and the tool the model answers through.

Two instructions here carry more weight than the rest put together, and both are
about `source_quote`: it must be copied rather than written, and it must stay in
the language of the source even when the question around it does not. Everything
downstream -- the page number, the highlight, the reader's trust that the answer
is really in the book -- rests on that one field being a literal slice of the text.
"""

# The tool is how the answer comes back. Asking for JSON in prose and parsing it
# is measurably less reliable; a schema the model fills in is checked on the way out.
QUESTION_TOOL = {
    "name": "save_questions",
    "description": "Save the questions written for this passage of the study material.",
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["mcq", "open"],
                            "description": "mcq = multiple choice with four options, "
                                           "open = answered in the reader's own words",
                        },
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Exactly 4 options, for type=mcq only. "
                                           "Omit entirely for open questions.",
                        },
                        "correct": {
                            "type": "string",
                            "description": "For mcq, one of the options copied word "
                                           "for word. For open, the model answer.",
                        },
                        "explanation": {
                            "type": "string",
                            "description": "One or two sentences on why that is the "
                                           "answer, and for mcq why the near miss is not.",
                        },
                        "source_quote": {
                            "type": "string",
                            "description": "A literal, contiguous run of the supplied "
                                           "text that the answer follows from. Copy it "
                                           "character for character. Do not translate, "
                                           "shorten, tidy or join separated passages.",
                        },
                    },
                    "required": ["type", "question", "correct", "source_quote"],
                },
            }
        },
        "required": ["questions"],
    },
}


SYSTEM_PROMPT = """You are an experienced university lecturer writing practice \
questions from a set course text, for one reader revising on their own.

You are not setting an exam paper. There are no sections, no marks, no question \
numbers and no rubric -- just a flat list of independent questions, each of which \
stands on its own and will be asked in isolation, in random order, weeks apart.

Ask only about what the supplied passage actually says. Add nothing from your own \
knowledge of the subject, however well you know it: a question the reader cannot \
answer from the text in front of them is worse than no question at all.

How many questions: judge it by the passage, between {min_questions} and \
{max_questions}. A dense page of definitions carries several; a page of running \
narrative carries one or two. A table of contents, an index, a title page, a \
bibliography or a page of nothing but figure captions carries none -- return an \
empty list and move on. Do not pad a thin passage to reach a number.

For multiple choice, write exactly four options. The distractors must be plausible \
and drawn from the same area as the answer -- neighbouring concepts, adjacent terms, \
the common misunderstanding. An option that is obviously absurd teaches nothing and \
gives the answer away. Copy the correct option into `correct` word for word, exactly \
as it appears in `options`.

Two habits give the answer away without either of us noticing, and both have to be \
avoided deliberately:

**Length.** Write the four options to roughly the same length -- within a few words \
of one another. The natural pull is to state the true option carefully and in full \
while writing the false ones tersely, which makes the longest option the right one. \
Read your four back before you finish: if one is noticeably longer, either trim it \
or say the others as fully. A reader who works out that the longest option wins has \
stopped learning the material.

**How a distractor is made wrong.** Make it wrong by *substituting a specific \
fact* -- the neighbouring structure, the opposite direction, the other transmitter, \
a swapped pair of attributes. Do not make it wrong by *overstating* something true.

This is the whole of the problem. Overstatement is the quickest way to write a \
false option -- "X has no role at all", "only Y does this", "all of them behave the \
same" -- so "always", "never", "only", "all", "none" and "cannot" end up living \
almost entirely in the wrong answers, and the reader learns to cross out whichever \
option sounds absolute. That rule works without knowing any of the subject.

Substitution does not have that problem, and makes a better question anyway, \
because the reader has to know which structure or which direction is right rather \
than notice which sentence sounds too strong. Compare:

  weak : "The thalamus is a passive relay with no modulatory role at all"
  good : "The thalamus relays sensory input directly to the spinal cord"

Both are false; only the second requires knowing anything. If an absolute is \
genuinely the point -- the passage itself says only one thing does something -- then \
use it, and it is fine for the correct option to be the absolute one. What must not \
happen is a bank where the absolute option is reliably the wrong one.

For open questions, `correct` is the model answer: what a well-prepared student \
would be expected to say, in two or three sentences.

`source_quote` is the passage the answer comes from, copied out of the supplied \
text character for character. It is not a summary and not a paraphrase. Keep it in \
{source_language}, the language of the source, whatever language you are writing the \
question in. Make it long enough to stand on its own -- a full sentence or two, \
around 15 to 40 words. If you cannot point at a literal run of text that contains \
the answer, do not ask the question.

Write the question, the options and the explanation in {question_language}.

You will not see page numbers and you are not asked for any.

{output_instruction}"""


# How the answer comes back, which differs by backend. The API path has a real
# tool schema; the CLI cannot be given one and is asked for JSON instead. The
# shared prompt used to tell both paths to call `save_questions`, so on the CLI
# the model was directed at a tool that does not exist -- and it duly ignored the
# instruction altogether and wrote prose.
TOOL_OUTPUT = "Return everything through the save_questions tool."

# Single braces: this is substituted into SYSTEM_PROMPT as a value, and
# str.format does not format its own substitutions, so doubling them here would
# put literal {{ in front of the model.
JSON_OUTPUT = """Return one JSON object and nothing else -- no preamble, no \
commentary, no markdown, no headings, no code fence.

The object has exactly one key, "questions", holding a flat array. Every entry \
has exactly these keys and no others:

  "type"          "mcq" or "open". Those two strings exactly.
  "question"      the question text
  "options"       an ARRAY of exactly 4 strings. Only for "mcq"; omit for "open".
  "correct"       for mcq, one of the options copied word for word -- the option
                  text itself, never a letter or an index. For open, the model
                  answer in two or three sentences.
  "explanation"   one or two sentences
  "source_quote"  the literal passage the answer comes from

Do not add sections, marks, ids, numbering, instructions, difficulty or any other \
key. Do not nest the questions inside sections. Do not write "options" as an \
object keyed by letters.

Example of the whole reply:

{"questions": [{"type": "mcq", "question": "...", "options": ["...", "...", \
"...", "..."], "correct": "...", "explanation": "...", "source_quote": "..."}, \
{"type": "open", "question": "...", "correct": "...", "explanation": "...", \
"source_quote": "..."}]}

A passage that warrants no questions returns {"questions": []}."""


USER_TEMPLATE = """Here is the passage.{chapter}

<text>
{text}
</text>

Write the questions for this passage."""


def system_prompt(source_language: str, question_language: str,
                  min_questions: int = 2, max_questions: int = 8,
                  output_instruction: str = TOOL_OUTPUT) -> str:
    return SYSTEM_PROMPT.format(
        source_language=source_language,
        question_language=question_language,
        min_questions=min_questions,
        max_questions=max_questions,
        output_instruction=output_instruction,
    )


def user_message(text: str, title: str | None = None) -> str:
    """The chunk itself.

    The chapter title is given as context but pointedly not as something to ask
    about -- it tells the model what the passage is for without inviting a
    question whose answer is only in the heading.
    """
    chapter = f" It comes from the section titled \"{title}\"." if title else ""
    return USER_TEMPLATE.format(chapter=chapter, text=text)


def json_reminder(min_questions: int = 2, max_questions: int = 8) -> str:
    """The whole output contract, repeated after the passage.

    In the system prompt alone it lost, twice. Two thousand words of
    neurophysiology arrive between the rule and the answer, and the model
    reverted to what "write exam questions" usually means: first a markdown exam
    paper, then JSON in its own vocabulary -- `multiple_choice`, `marks`,
    `difficulty`, options keyed by letter, and 23 questions where 8 were asked
    for.

    So the contract is restated here, last, where nothing comes between it and
    the reply. Repetition in a prompt is not redundancy when the thing being
    repeated has already been overridden once.
    """
    return f"""

Now write the questions.

Reply with ONE JSON object and nothing else. No preamble, no markdown, no
headings, no code fence, no closing question. Start with {{ and end with }}.

{{"questions": [ ... ]}}

Each entry in the array has EXACTLY these keys, spelled exactly like this:

  "type"          either "mcq" or "open" -- these two strings only. Not
                  "multiple_choice", not "short_answer", not "essay".
  "question"      the question text
  "options"       an ARRAY of exactly 4 strings, for "mcq" only. Not an object
                  keyed by "A"/"B"/"C"/"D". Omit the key entirely for "open".
  "correct"       for "mcq", the full text of the correct option copied word for
                  word from "options" -- never a letter, never an index.
                  For "open", the model answer in two or three sentences.
  "explanation"   one or two sentences
  "source_quote"  a literal, contiguous run of the passage above, copied
                  character for character

No other keys. No "id", "marks", "difficulty", "section" or "answer".
No sections, no grouping, no rubric: one flat array.
Between {min_questions} and {max_questions} entries in total, judged by how much
the passage actually carries. Return {{"questions": []}} if it carries nothing.

Before you finish, look at the four options of every mcq side by side:

  - Are they about the same length? The correct one must not be the longest.
    Measured over a real run, it was the longest 58% of the time against 25%
    by chance, which let a reader score 58% by picking the longest and reading
    nothing. Trim it, or say the distractors as fully.
  - Is each wrong option wrong because a specific fact was swapped -- the other
    structure, the opposite direction, the other transmitter -- rather than
    because a true statement was overstated into "has no role at all" or "only
    X does this"? Overstatement is the quick way to write a false option, and it
    parks "always", "never", "only", "all", "none" and "cannot" in the wrong
    answers, where the reader learns to cross out whichever one sounds absolute.
    Rewrite any distractor that is only wrong because it is too strong."""
