from skopax import tags

TEAM_TAGS = {'bizdev': 'Business Development', 'marketing': 'Marketing', 'seo': 'SEO', 'product': 'Product Team', tags.DEVELOPER: 'Developer', tags.DATA: 'Data', tags.INFRA: 'Infra',
             tags.ANALYST: 'Analyst'}
TEAM_RULES = (('bizdev', ('type:page-typos',)),
              ('marketing', ('type:gtm', 'type:umami', 'type:ga', 'type:ad-link*', 'type:ab-test', 'type:mailer')),
              ('seo', ('gone', 'type:crux', 'type:safebrowsing')),
              ('product', ('type:store-rating',)),
              (tags.DEVELOPER, ('type:sonar-gate', 'type:cve-deps')),
              (tags.DATA, ('type:airbyte', 'alert|*|cluster=east-data-prometheus*')),
              (tags.INFRA, ('type:cve-os',)),
              (tags.ANALYST, ()))


def apply_team_tags() -> None:
    tags.TAGS.clear()
    tags.TAGS.update(TEAM_TAGS)
    tags.RULES = TEAM_RULES
