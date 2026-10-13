"""StudyApp -- a quiz trainer built from one PDF of study material.

Generating the question bank is a one-off online batch; everything after that runs
offline. The two halves share only the database, so nothing in the runtime path
imports the generator.
"""

__version__ = "0.1.0"
