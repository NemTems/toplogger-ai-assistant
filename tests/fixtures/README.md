# Fixtures

Sanitised recordings of TopLogger GraphQL responses, committed deliberately so
tests never hit the live API (hard rule 6).

- Never point a test at the network. To add a fixture, hand-edit a response
  you captured while manually using the app/adapter, then commit the edited
  copy — don't script a live capture into this directory.
- Sanitise before committing: strip or replace any other user's name, user
  ID, avatar URL or per-person tick record (hard rule 4). Only aggregated
  counts and your own data may appear.
- No access token, refresh token, password or reCAPTCHA token ever belongs in
  a fixture (hard rule 1).
