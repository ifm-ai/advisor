# Security policy

Advisor keeps privileged task material (reference solutions, hidden tests, private notes) away from
the solver it advises. A way around that is a security problem, and we want to hear about it.

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Please do not open a public issue. Include
what you ran, what you expected, and what happened; a minimal task directory or trajectory helps.

## In scope

- Any path by which privileged material, or the advisor's private output (its diagnosis, plan, or
  an unscreened hint), reaches the solver's context, a trajectory, or a file the documentation
  calls publishable.
- `ifm-advisor audit` reporting `pass` or `pass_mechanical_only` (or exiting 0) for a trajectory
  that its documented checks should catch (a verbatim privileged line of 40 or more characters, a
  listed leak term, a judge finding), or for an input it could not fully read or parse.
- The loader reading outside the task directory, following a symlink, or blocking on a special
  file. Symlinks are checked statically, before reading: a symlink swapped into an intermediate
  directory while a load is running is a known limitation, not a supported guarantee.

## Out of scope

- Model behavior the prompts cannot guarantee. A reasoning-prefill hint is written by a model and
  is guarded, not guaranteed, solver-safe; see the isolation documentation.
- Copies the audit's mechanical checks are documented not to catch: paraphrase, lines shorter than
  40 characters, homoglyphs or zero-width characters, and text encoded deeper than the documented
  decoding. These are documented limits of the mechanical checks, and the judges exist for them.
- Files the documentation says never to publish, such as `advisor/private_trace.jsonl`, once they
  have been published.
