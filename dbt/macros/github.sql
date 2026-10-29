{# Commit author emails that are mine. Connection builds pass the analysis parameter
   github_author_emails; legacy builds read GITHUB__AUTHOR_EMAILS (fnox; kept out of
   this public repo). Renders a SQL list; never empty so `in (...)` stays valid. #}
{% macro github_my_emails() -%}
    {%- set emails = var('github_author_emails', none) -%}
    {%- if emails is none -%}
        {%- set emails = fromjson(env_var('GITHUB__AUTHOR_EMAILS', '[]')) -%}
    {%- endif -%}
    ({%- for e in emails %}'{{ (e | lower) | replace("'", "''") }}', {% endfor -%}'-')
{%- endmacro %}

{# Bot accounts: GitHub's user type, the [bot] suffix, or the export's bot list #}
{% macro github_is_bot(login, type) -%}
    coalesce({{ type }} = 'Bot'
        or {{ login }} like '%[bot]'
        or {{ login }} in (select login from {{ ref('stg_github__bots') }}), false)
{%- endmacro %}
