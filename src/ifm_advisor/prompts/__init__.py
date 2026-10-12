"""Versioned advisor system prompts and solver profiles.

Prompts ship inside the package so an installed wheel can read them. A released prompt or profile
file is never edited: a change adds a new version file, selected with
`AdvisorConfig.prompt_version`, because a changed prompt changes advisor behavior and must be
traceable in results. `tests/fixtures/pre_submit_v1.txt` is the prompt used for the paper's runs.
"""
