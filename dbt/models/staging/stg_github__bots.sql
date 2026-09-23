-- Bot accounts listed in the export (some older ones lack the [bot] suffix)
select login from {{ source('github', 'export_bots') }}
