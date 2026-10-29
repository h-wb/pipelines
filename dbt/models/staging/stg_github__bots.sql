-- Bot accounts listed in the export (some older ones lack the [bot] suffix)
with export_bots as (
    {{ optional_input('github', 'export_bots', {'login': 'text'}, ['login']) }}
)
select login from export_bots
