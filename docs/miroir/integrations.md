# Writing an integration

An integration is an ordinary dlt source plus a small Miroir definition. It does
not manage pipelines, destinations, Prefect deployments, secrets, or web routes.
Those belong to core. See decision 026 for the ownership boundary.

## Package structure

Start with `src/integrations/<name>/__init__.py` and `source.py`. Keep small
connectors small. For larger ones, use `resources/<resource>.py` and a shared
`client.py`; GitHub is the bundled example.

The definition module must be importable without dlt, Prefect, a database, or
network access. Declare a strict settings model, credential fields, resource
presentation, a fetch and/or file formats, and optionally a check and recommended
analysis package. Import execution dependencies inside the input opener. Add the
definition to `src/integrations/registry.py`.

Settings are persisted, so never put tokens in the settings model. Input openers
receive already-resolved credentials in memory. Open sessions and temporary files
inside a context manager and close them on both success and failure.

## Native dlt execution

Use the simplest suitable dlt primitive:

- `rest_api_resources` / `rest_api_source` for declarative REST endpoints,
  pagination, incremental configuration, and parent-child relationships.
- `@dlt.resource` and `RESTClient` for custom extraction, GraphQL, or unusual
  pagination. ListenBrainz demonstrates a custom paginator; GitHub demonstrates
  search windows and commit enrichment.
- `@dlt.transformer` when records from a parent resource drive child extraction.
  dlt owns the extraction graph; do not introduce a second dependency resolver.

Compose resources in a thin `@dlt.source`. Source construction must not fetch
data: resource generators do that during extraction. Share client configuration
and per-run discovery where useful, but not mutable state between source instances.
Resource keys, write disposition, cursor handling, and table hints live in dlt
code, not Miroir's metadata. Keep extraction close to upstream records; reporting
transformations belong in analysis packages.

Use `IntegrationError` from `src/integrations/errors.py` for safe, classified
credential-rejection, rate-limit, and unexpected-response diagnostics. The runner
recognizes these even when dlt wraps them. Never include tokens, request URLs, or
remote response bodies in these messages. Keep original HTTP exceptions when they
are needed for retry/classification; do not classify every 403 as a bad token.

A resource is a logical stream such as commits, not necessarily one endpoint or
one output table. Commit extraction may discover repositories and branches and
then fetch details. Supporting requests are not separate connection checkboxes.
Native dlt parent resources can likewise run for a selected child without loading
their own tables. Avoid destructive writes unless the connector explicitly needs
and documents them; merge resources usually retain previously observed history.

## Incremental state

Prefer `dlt.sources.incremental` on a record field, with `lag` to re-read a recent
overlap for late updates, over hand-written watermarks. Make sure the stream's
ordering actually supports what you rely on. A transformer's records are ordered
per parent item at most, not across the whole stream, so stop pagination early only
within one parent. Keep custom resource state when record dates do not show when an
item became visible upstream. For example, an old-dated commit can be pushed or
indexed later, so GitHub's commit and search resources keep run windows.

Replacing an existing watermark with a native cursor is a state migration. Seed the
untouched native cursor from the old key inside extraction, so a failed run rolls
the seed back with the rest of the state. Do not advance the old key afterward.
`src/integrations/github/incremental.py` does this for issues, comments, pull
requests and the contribution calendar (decision 028).

## Presentation and selection

`ResourceDefinition` declares a stable `name`, a human-readable `label`, a
`description`, an optional `warning`, and `record_kind` / `history` explanations.
Names match the selectable dlt resources. Descriptions should explain scope and
permissions; warnings should mention expensive extraction or upstream history
limits. No execution callable belongs in this metadata.

Fetched resources appear automatically in connection setup and settings. An
omitted or null connection `resources` field means all declared resources. An
explicit list is nonempty, unique, and validated. Saving the checkboxes pins that
list, so future additions are not silently selected for that connection. Existing
implicit-all connections continue to include new resources.

Deselecting does not drop data or reset state; reselecting resumes the existing
cursor. Analysis can still read retained rows, so selection is not deletion or a
guarantee of analysis freshness per resource. File imports currently parse all
their resources; the fetched-resource selector does not filter artifacts.

## Files and delivery

A `FileFormat` declares validation, parser version, typed per-import options, and
an opener over one retained artifact. Upload, receiving URL, and watched folder
are core transports, not connector implementations. Bump the parser version when
a change alters rows or values produced by an artifact.

## Compatibility and tests

Source/resource names, table names, primary keys, and state keys are persistent
identities. Preserve them during code moves; changing them needs an explicit data
and state migration, not just a Python rename.

Use unittest with intercepted HTTP and real dlt loads into temporary DuckDB.
Cover selected resources, supporting requests without unwanted tables, merge
idempotence, cursor preservation across deselect/reselect, and failed extraction
without committed cursor advancement. Test that source assembly performs no
requests and that declared resource names match actual selectable resources.
`tests/test_github_resources.py` provides an example. Never use real account tokens
or the production warehouse in these tests.

The bundled registry is deliberately explicit. External package installation and
discovery are not implemented; installing Python integrations will require trusting
their code, not treating them as sandboxed configuration.
