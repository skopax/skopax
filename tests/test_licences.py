from pathlib import Path

from tools import licences


MIT_TEXT = 'Permission is hereby granted, free of charge, to any person obtaining a copy of this software.\n'
TWO_CLAUSE = ('Redistribution and use in source and binary forms, with or without modification, are permitted provided that:\n'
              '1. Redistributions of source code must retain the above copyright notice.\n'
              '2. Redistributions in binary form must reproduce the above copyright notice.\n')
THREE_CLAUSE = TWO_CLAUSE + '3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse products.\n'
FOUR_CLAUSE = TWO_CLAUSE + ('3. All advertising materials mentioning features or use of this software must display an acknowledgement.\n'
                            '4. Neither the name of the University nor the names of its contributors may be used to endorse products.\n')
GPL_TEXT = 'GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n'


def dist(site: Path, name: str, version: str, headers: tuple[str, ...] = (), texts: dict[str, str] | None = None, legacy: dict[str, str] | None = None, unlisted: dict[str, str] | None = None) -> Path:
    folder = site / f'{name.replace("-", "_")}-{version}.dist-info'
    (folder / 'licenses').mkdir(parents=True)
    lines = ['Metadata-Version: 2.4', f'Name: {name}', f'Version: {version}', *headers]
    for file, text in (texts or {}).items():
        lines.append(f'License-File: {file}')
        (folder / 'licenses' / file).write_text(text, encoding='utf-8')
    for file, text in (legacy or {}).items():
        lines.append(f'License-File: {file}')
        (folder / file).write_text(text, encoding='utf-8')
    for file, text in (unlisted or {}).items():
        (folder / file).parent.mkdir(parents=True, exist_ok=True)
        (folder / file).write_text(text, encoding='utf-8')
    if unlisted:
        record = [f'{folder.name}/{file},,' for file in ('METADATA', *unlisted)]
        (folder / 'RECORD').write_text('\n'.join(record) + '\n', encoding='utf-8')
    (folder / 'METADATA').write_text('\n'.join(lines) + '\n\n', encoding='utf-8')
    return folder


def mit(site: Path, name: str, version: str, headers: tuple[str, ...]) -> Path:
    return dist(site, name, version, headers, texts={'LICENSE': MIT_TEXT})


def overrides(tmp_path: Path, text: str = '', name: str = 'overrides.toml') -> Path:
    path = tmp_path / name
    path.write_text(text, encoding='utf-8')
    return path


def check(tmp_path: Path, site: Path, rules: Path) -> int:
    return licences.main(['check', '--site', str(site), '--out', str(tmp_path / 'out'), '--overrides', str(rules)])


def listed(tmp_path: Path) -> str:
    return (tmp_path / 'out' / 'third-party-python.md').read_text(encoding='utf-8')


def test_an_allowed_licence_passes_and_is_listed(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit(site, 'alpha', '1.0.0', ('License-Expression: MIT',))
    dist(site, 'beta', '2.1', ('Classifier: License :: OSI Approved :: BSD License',), texts={'LICENSE': THREE_CLAUSE})
    dist(site, 'gamma-lib', '3.0', ('License: Apache-2.0',))
    mit(site, 'delta', '0.4', ('License: MIT OR Apache-2.0', 'Classifier: License :: OSI Approved :: MIT License', 'Classifier: License :: OSI Approved :: Apache Software License'))
    mit(site, 'epsilon', '1.0', ('License-Expression: GPL-3.0-only OR MIT',))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 0
    assert capsys.readouterr().out == ''
    text = listed(tmp_path)
    assert '| alpha | 1.0.0 | MIT |  |' in text
    assert '| beta | 2.1 | BSD-3-Clause |  |' in text
    assert '| gamma-lib | 3.0 | Apache-2.0 |  |' in text
    assert '| delta | 0.4 | MIT OR Apache-2.0 | used under MIT |' in text
    assert '| epsilon | 1.0 | GPL-3.0-only OR MIT | used under MIT |' in text


def test_a_lower_source_that_names_a_refused_licence_refuses_the_dist(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit(site, 'gamma-lib', '3.0', ('License: Apache-2.0', 'Classifier: License :: OSI Approved :: GNU General Public License v3 (GPLv3)'))
    mit(site, 'kappa', '1.0', ('License-Expression: MIT', 'License: GPL-3.0-only'))
    mit(site, 'compound', '1.0', ('License: MIT AND GPL-3.0-only', 'Classifier: License :: OSI Approved :: MIT License'))
    mit(site, 'agreeing', '1.0', ('License-Expression: MIT', 'License: MIT', 'Classifier: License :: OSI Approved :: MIT License'))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['compound 1.0: MIT AND GPL-3.0-only', 'gamma-lib 3.0: Apache-2.0; also declares GPL-3.0-only', 'kappa 1.0: MIT; also declares GPL-3.0-only']


def test_a_prose_licence_field_that_names_refused_terms_refuses_the_dist(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit_classifier = 'Classifier: License :: OSI Approved :: MIT License'
    mit(site, 'gnu-field', '1.0', ('License: GNU GPL v3', mit_classifier))
    mit(site, 'gpl-field', '1.0', ('License-Expression: MIT', 'License: GPLv3'))
    mit(site, 'owned-field', '1.0', ('License: Proprietary', mit_classifier))
    mit(site, 'sold-field', '1.0', ('License: Commercial licence, see LICENSE', mit_classifier))
    mit(site, 'affero-field', '1.0', ('License: Affero terms', mit_classifier))
    mit(site, 'prose-ok', '1.0', ('License: MIT License', mit_classifier))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['affero-field 1.0: MIT; licence field names Affero terms', 'gnu-field 1.0: MIT; licence field names GNU GPL v3',
                                                    'gpl-field 1.0: MIT; licence field names GPLv3', 'owned-field 1.0: MIT; licence field names Proprietary',
                                                    'sold-field 1.0: MIT; licence field names Commercial licence, see LICENSE']


def test_more_prose_licence_terms_refuse_the_dist_and_plain_prose_stays_green(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit_classifier = 'Classifier: License :: OSI Approved :: MIT License'
    refused = {'copyleft': 'Copyleft', 'gnu': 'GNU', 'hyphenated': 'Non-Commercial use only', 'joined': 'Noncommercial', 'reserved': 'All  Rights   Reserved',
               'cddl': 'CDDL', 'sspl': 'SSPL', 'eclipse': 'Eclipse terms', 'busl': 'Business Source License 1.1', 'cc-nc': 'Creative Commons BY-NC',
               'closed': 'Closed source', 'cc-dash-nc': 'CC-BY-NC 4.0', 'nc-word': 'Data under NC terms', 'eupl': 'EUPL 1.2', 'osl': 'OSL 3.0 terms',
               'ms-rl': 'MS-RL terms', 'elastic': 'Elastic License 2.0', 'cpal': 'CPAL 1.0', 'fsf': 'Free Software Foundation', 'busl-hyphen': 'Business-Source License',
               'wide-nc': 'CC-BY-\uff2eC 4.0', 'osl-name': 'Open Software License 3.0', 'ms-rl-name': 'Microsoft Reciprocal License',
               'cpal-name': 'Common Public Attribution License 1.0', 'unlicensed': 'UNLICENSED'}
    plain = {'cc-by': 'Creative Commons Attribution 4.0', 'cc-licence': 'Creative Commons Attribution licence', 'since': 'Since 2020, licensed as MIT', 'bsd': 'BSD', 'three-bsd': '3-Clause BSD License', 'mit-prose': 'MIT License',
             'isc-prose': 'ISC License Copyright (c) 2024, the contributors. Permission to use, copy, modify, and/or distribute this software for any purpose',
             'apache-prose': 'Apache 2.0'}
    for name, text in {**refused, **plain}.items():
        mit(site, name, '1.0', (f'License: {text}', mit_classifier))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [f'{name} 1.0: MIT; licence field names {" ".join(refused[name].split())}' for name in sorted(refused)]


def test_a_long_licence_field_is_judged_as_a_licence_text(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    bsd = 'Classifier: License :: OSI Approved :: BSD License'
    mit_classifier = 'Classifier: License :: OSI Approved :: MIT License'
    full_bsd = ' '.join(('BSD 3-Clause License Copyright (c) 2010, the authors. All rights reserved.', *THREE_CLAUSE.split('\n')))
    filler = ' This text is long enough to be read as a licence text and not as a short name of a licence.' * 2
    dist(site, 'bsd-field', '1.0', (f'License: {full_bsd}', bsd), texts={'LICENSE': THREE_CLAUSE})
    mit(site, 'gpl-field', '1.0', (f'License: GNU GENERAL PUBLIC LICENSE Version 3.{filler}', mit_classifier))
    mit(site, 'lgpl-field', '1.0', (f'License: GNU LESSER GENERAL PUBLIC LICENSE Version 2.1.{filler}', mit_classifier))
    mit(site, 'ads-field', '1.0', (f'License: {FOUR_CLAUSE.replace(chr(10), " ")}{filler}', mit_classifier))
    mit(site, 'short-field', '1.0', ('License: All rights reserved', mit_classifier))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert len(full_bsd) > 200 and len(filler) > 180
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['ads-field 1.0: MIT; licence field text names advertising materials', 'gpl-field 1.0: MIT; licence field text names gnu general public license',
                                                    'lgpl-field 1.0: MIT; licence field text names general public license', 'short-field 1.0: MIT; licence field names All rights reserved']
    assert {e.name: e.licence for e in licences.survey(site, licences.Overrides())}['bsd-field'] == 'BSD-3-Clause'


FULL_MIT = ('MIT License Copyright (c) 2010 the contributors. Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated '
            'documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, '
            'distribute, sublicense, and/or sell copies of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.')
FULL_ISC = ('ISC License Copyright (c) 2024, the contributors. Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby '
            'granted, provided that the above copyright notice and this permission notice appear in all copies. THE SOFTWARE IS PROVIDED "AS IS".')
FULL_BSD3 = ('BSD 3-Clause License Copyright (c) 2015, the authors. All rights reserved. Redistribution and use in source and binary forms, with or without modification, '
             'are permitted provided that the following conditions are met: 1. Redistributions of source code must retain the above copyright notice. 2. Redistributions in '
             'binary form must reproduce the above copyright notice. 3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse products.')
FULL_APACHE = ('Apache License Version 2.0, January 2004 http://www.apache.org/licenses/ TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION 1. Definitions. '
               '8. Limitation of Liability. In no event shall any Contributor be liable to You for damages, including but not limited to damages for loss of goodwill, '
               'work stoppage, computer failure or malfunction, or any and all other commercial damages or losses.')
APACHE_NOTICE = ('Copyright 2020 the authors. Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with the License. '
                 'You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0')
ELASTIC_TEXT = ('Elastic License 2.0 (ELv2). Acceptance. By using the software, you agree to all of the terms and conditions below. Limitations. You may not provide the software '
                'to third parties as a hosted or managed service, where the service provides users with access to any substantial set of the features or functionality of the software.')
PROPRIETARY_TEXT = ('Copyright (c) 2026 Example Corp. This software is proprietary and confidential. Unauthorized copying of this file, via any medium, is strictly prohibited. '
                    'You may not redistribute, sublicense or modify it without a written agreement signed by Example Corp.')
BUSL_TEXT = ('Business Source License 1.1. Parameters. Licensor: Example. Licensed Work: Thing. Additional Use Grant: None. Change Date: 2030-01-01. Change License: Apache-2.0. '
             'The Licensor hereby grants you the right to copy, modify, create derivative works, redistribute, and make non-production use of the Licensed Work.')
UNKNOWN_TEXT = 'licence field is a text that is not a known allowed licence'


def test_a_long_licence_field_that_is_not_a_known_allowed_text_refuses_the_dist(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit_classifier = 'Classifier: License :: OSI Approved :: MIT License'
    for name, text in (('elastic', ELASTIC_TEXT), ('owned', PROPRIETARY_TEXT), ('busl', BUSL_TEXT)):
        mit(site, name, '1.0', (f'License: {text}', mit_classifier))
    mit(site, 'expr-owned', '1.0', ('License-Expression: MIT', f'License: {PROPRIETARY_TEXT}'))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert min(len(t) for t in (ELASTIC_TEXT, PROPRIETARY_TEXT, BUSL_TEXT)) > 200
    assert code == 1
    assert capsys.readouterr().out.splitlines() == [f'{name} 1.0: MIT; {UNKNOWN_TEXT}' for name in ('busl', 'elastic', 'expr-owned', 'owned')]


def test_a_known_allowed_text_with_a_restricting_rider_refuses_the_dist(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit_classifier = 'Classifier: License :: OSI Approved :: MIT License'
    apache_classifier = 'Classifier: License :: OSI Approved :: Apache Software License'
    riders = {
        'mit-owned': (FULL_MIT + ' Notwithstanding the above, this software is proprietary and confidential; you may not redistribute it without a signed agreement.', mit_classifier),
        'apache-sell': (APACHE_NOTICE + ' "Commons Clause" License Condition v1.0. The grant of rights under the License will not include the right to Sell the Software.', apache_classifier),
        'mit-hosted': (FULL_MIT + ' You may not provide the Software to third parties as a hosted or managed service.', mit_classifier),
    }
    for name, (text, classifier) in riders.items():
        mit(site, name, '1.0', (f'License: {text}', classifier))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert min(len(text) for text, _ in riders.values()) > 200
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['apache-sell 1.0: Apache-2.0; licence field text names commons clause', 'mit-hosted 1.0: MIT; licence field text names hosted or managed service',
                                                    'mit-owned 1.0: MIT; licence field text names proprietary']


def test_a_long_licence_field_passes_when_it_reads_as_a_known_allowed_text(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    dist(site, 'mit-text', '1.0', (f'License: {FULL_MIT}', 'Classifier: License :: OSI Approved :: MIT License'), texts={'LICENSE': FULL_MIT})
    dist(site, 'isc-text', '1.0', (f'License: {FULL_ISC}', 'Classifier: License :: OSI Approved :: ISC License (ISCL)'), texts={'LICENSE': FULL_ISC})
    dist(site, 'bsd-text', '1.0', (f'License: {FULL_BSD3}', 'Classifier: License :: OSI Approved :: BSD License'), texts={'LICENSE': FULL_BSD3})
    dist(site, 'apache-text', '1.0', (f'License: {FULL_APACHE}', 'Classifier: License :: OSI Approved :: Apache Software License'))
    dist(site, 'apache-notice', '1.0', (f'License: {APACHE_NOTICE}', 'Classifier: License :: OSI Approved :: Apache Software License'))

    # when
    code = check(tmp_path, site, overrides(tmp_path))
    found = {e.name: e.licence for e in licences.survey(site, licences.Overrides())}

    # then
    assert min(len(t) for t in (FULL_MIT, FULL_ISC, FULL_BSD3, FULL_APACHE, APACHE_NOTICE)) > 200
    assert (code, capsys.readouterr().out) == (0, '')
    assert found == {'mit-text': 'MIT', 'isc-text': 'ISC', 'bsd-text': 'BSD-3-Clause', 'apache-text': 'Apache-2.0', 'apache-notice': 'Apache-2.0'}


def test_an_override_rescues_a_long_licence_field_that_is_not_a_known_text(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit(site, 'owned', '1.0', (f'License: {PROPRIETARY_TEXT}', 'Classifier: License :: OSI Approved :: MIT License'))
    rules = overrides(tmp_path, '[python.owned]\nversion = "1.0"\nlicence = "MIT"\nreason = "the shipped LICENSE is the MIT text; the field is a stale header"\n')

    # when
    plain = check(tmp_path, site, overrides(tmp_path, '', 'empty.toml'))
    plain_out = capsys.readouterr().out
    reviewed = check(tmp_path, site, rules)

    # then
    assert (plain, plain_out.splitlines()) == (1, [f'owned 1.0: MIT; {UNKNOWN_TEXT}'])
    assert (reviewed, capsys.readouterr().out) == (0, '')
    assert '| owned | 1.0 | MIT | reviewed: the shipped LICENSE is the MIT text; the field is a stale header |' in listed(tmp_path)


def test_a_licence_text_that_is_not_utf8_is_read_with_replacement(tmp_path, capsys):
    # given
    listed_site, unlisted_site = tmp_path / 'listed', tmp_path / 'unlisted'
    folder = dist(listed_site, 'latin', '1.0', ('License-Expression: MIT',), texts={'LICENSE': 'x'})
    (folder / 'licenses' / 'LICENSE').write_bytes(b'Copyright (c) Ren\xe9\nPermission is hereby granted\n')
    other = dist(unlisted_site, 'latin', '1.0', ('License: BSD',), unlisted={'licenses/LICENSE': 'x'})
    (other / 'licenses' / 'LICENSE').write_bytes(b'Copyright (c) Ren\xe9\nPermission is hereby granted\n')
    rules = overrides(tmp_path, '[python.latin]\nversion = "1.0"\nlicence = "MIT"\nreason = "read upstream"\n', 'latin.toml')

    # when
    plain = check(tmp_path, listed_site, overrides(tmp_path))
    plain_text = listed(tmp_path)
    reviewed = check(tmp_path, unlisted_site, rules)
    reviewed_text = listed(tmp_path)

    # then
    assert (plain, reviewed, capsys.readouterr().out) == (0, 0, '')
    assert 'Copyright (c) Ren\ufffd\nPermission is hereby granted' in plain_text
    assert 'Copyright (c) Ren\ufffd\nPermission is hereby granted' in reviewed_text


def test_a_repeated_distribution_or_licence_header_is_refused(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit(site, 'dup', '1.0', ('License-Expression: MIT',))
    mit(site, 'dup', '2.0', ('License-Expression: GPL-3.0-only',))
    mit(site, 'Dot.Name', '1.0', ('License-Expression: MIT',))
    dist(site, 'dot-name', '1.0', ('License-Expression: AGPL-3.0-only',), legacy={'LICENSE': MIT_TEXT})
    mit(site, 'two-expressions', '1.0', ('License-Expression: MIT', 'License-Expression: GPL-3.0-only'))
    mit(site, 'two-fields', '1.0', ('License-Expression: MIT', 'License: MIT', 'License: GPL-3.0-only'))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['dot-name: more than one distribution', 'dup: more than one distribution',
                                                    'two-expressions 1.0: MIT; more than one License-Expression header', 'two-fields 1.0: MIT; more than one License header']


def test_any_licence_classifier_outside_the_map_is_unknown(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit(site, 'owned', '1.0', ('Classifier: License :: Other/Proprietary License', 'Classifier: License :: OSI Approved :: MIT License'))
    mit(site, 'given-away', '1.0', ('Classifier: License :: Public Domain',))
    mit(site, 'dedicated', '1.0', ('License-Expression: MIT', 'Classifier: License :: CC0 1.0 Universal (CC0 1.0) Public Domain Dedication'))
    mit(site, 'free', '1.0', ('License: MIT', 'Classifier: License :: Freely Distributable'))
    mit(site, 'spaced', '1.0', ('Classifier: License :: OSI Approved :: MIT License ',))
    mit(site, 'lower', '1.0', ('Classifier: license :: OSI Approved :: GNU General Public License v3 (GPLv3)', 'Classifier: License :: OSI Approved :: MIT License'))
    mit(site, 'squashed', '1.0', ('Classifier: License ::OSI Approved :: GNU General Public License v3 (GPLv3)', 'Classifier: License :: OSI Approved :: MIT License'))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['dedicated 1.0: MIT; unmapped licence classifier', 'free 1.0: MIT; unmapped licence classifier', 'given-away 1.0: unknown',
                                                    'lower 1.0: unknown', 'owned 1.0: unknown', 'squashed 1.0: unknown']


def test_a_bare_bsd_classifier_is_named_by_the_shipped_text(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    bsd = 'Classifier: License :: OSI Approved :: BSD License'
    dist(site, 'three', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE})
    dist(site, 'two', '1.0', ('License: BSD', bsd), legacy={'LICENSE': TWO_CLAUSE})
    dist(site, 'textless', '1.0', (bsd,))
    dist(site, 'gpl-copy', '1.0', (bsd,), texts={'COPYING': GPL_TEXT})
    dist(site, 'mixed-texts', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'COPYING': GPL_TEXT})
    dist(site, 'four', '1.0', (bsd,), texts={'LICENSE': FOUR_CLAUSE})
    dist(site, 'not-bsd', '1.0', (bsd,), texts={'LICENSE': MIT_TEXT})
    dist(site, 'with-cddl', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'CDDL': 'COMMON DEVELOPMENT AND DISTRIBUTION LICENSE Version 1.0\n'})
    dist(site, 'with-epl', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'EPL': 'Eclipse Public License - v 2.0\n'})
    dist(site, 'with-data', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'DATA': 'This data may not be used for commercial purposes.\n'})
    dist(site, 'with-nc', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'FONTS': 'Fonts under a non-commercial licence.\n'})
    dist(site, 'with-lgpl', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'COPYING.LESSER': 'GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1, February 1999\n'})
    dist(site, 'with-lgpl-unlisted', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE}, unlisted={'licenses/COPYING.LESSER': 'GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3\n'})
    dist(site, 'with-lib-gpl', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'COPYING.LIB': 'GNU LIBRARY GENERAL PUBLIC LICENSE\nVersion 2, June 1991\n'})
    dist(site, 'with-agpl', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'COPYING': 'GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3\n'})
    dist(site, 'with-sspl', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE, 'COPYING': 'Server Side Public License\nVERSION 1, OCTOBER 16, 2018\n'})

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['four 1.0: unknown', 'gpl-copy 1.0: unknown', 'mixed-texts 1.0: unknown', 'not-bsd 1.0: unknown', 'textless 1.0: unknown',
                                                    'with-agpl 1.0: unknown', 'with-cddl 1.0: unknown', 'with-data 1.0: unknown', 'with-epl 1.0: unknown', 'with-lgpl 1.0: unknown',
                                                    'with-lgpl-unlisted 1.0: unknown', 'with-lib-gpl 1.0: unknown', 'with-nc 1.0: unknown', 'with-sspl 1.0: unknown']
    survey = {e.name: e.licence for e in licences.survey(site, licences.Overrides())}
    assert (survey['three'], survey['two']) == ('BSD-3-Clause', 'BSD-2-Clause')


def test_a_textless_dist_passes_only_under_a_licence_that_needs_no_text(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    dist(site, 'apache-only', '1.0', ('License-Expression: Apache-2.0',))
    dist(site, 'psf-only', '1.0', ('License-Expression: PSF-2.0',))
    dist(site, 'mpl-only', '1.0', ('License-Expression: MPL-2.0',))
    dist(site, 'either', '1.0', ('License-Expression: MIT OR Apache-2.0',))
    dist(site, 'mit-less', '1.0', ('License-Expression: MIT',))
    dist(site, 'joined', '1.0', ('License-Expression: Apache-2.0 AND ISC',))
    dist(site, 'blank-notice', '1.0', ('License-Expression: MIT',), texts={'NOTICE': '  \n'})
    (tmp_path / 'mit-less-LICENSE').write_text(MIT_TEXT, encoding='utf-8')
    entry = '[python.mit-less]\nversion = "1.0"\nlicence = "MIT"\nreason = "read upstream"\n'
    textless = overrides(tmp_path, entry, 'textless.toml')
    vendored = overrides(tmp_path, entry + 'text = "mit-less-LICENSE"\n[python.blank-notice]\nversion = "1.0"\nlicence = "MIT"\nreason = "read upstream"\ntext = "mit-less-LICENSE"\n[python.joined]\nversion = "1.0"\nlicence = "Apache-2.0 AND ISC"\nreason = "read upstream"\ntext = "mit-less-LICENSE"\n', 'vendored.toml')

    # when
    bare = check(tmp_path, site, overrides(tmp_path))
    bare_out = capsys.readouterr().out
    reviewed = check(tmp_path, site, textless)
    reviewed_out = capsys.readouterr().out
    carried = check(tmp_path, site, vendored)

    # then
    assert (bare, bare_out.splitlines()) == (1, ['blank-notice 1.0: MIT; no licence text', 'joined 1.0: Apache-2.0 AND ISC; no licence text', 'mit-less 1.0: MIT; no licence text'])
    assert (reviewed, reviewed_out.splitlines()) == (1, ['blank-notice 1.0: MIT; no licence text', 'joined 1.0: Apache-2.0 AND ISC; no licence text', 'mit-less 1.0: MIT; no licence text'])
    assert carried == 0
    assert '| either | 1.0 | MIT OR Apache-2.0 | used under Apache-2.0 |' in listed(tmp_path)


def test_a_blank_vendored_text_counts_as_no_text(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    dist(site, 'mit-less', '1.0', ('License-Expression: MIT',))
    dist(site, 'apache-less', '1.0', ('License-Expression: Apache-2.0',))
    (tmp_path / 'blank-LICENSE').write_text(' \n\t\n', encoding='utf-8')
    rules = overrides(tmp_path, '[python.mit-less]\nversion = "1.0"\nlicence = "MIT"\nreason = "read upstream"\ntext = "blank-LICENSE"\n'
                                '[python.apache-less]\nversion = "1.0"\nlicence = "Apache-2.0"\nreason = "read upstream"\ntext = "blank-LICENSE"\n', 'blank.toml')

    # when
    code = check(tmp_path, site, rules)

    # then
    assert (code, capsys.readouterr().out.splitlines()) == (1, ['mit-less 1.0: MIT; no licence text'])
    assert {e.name: e.texts for e in licences.survey(site, licences.load_overrides(rules))}['apache-less'] == ()


def test_a_one_file_egg_info_is_refused_rather_than_skipped(tmp_path, capsys):
    # given
    site, alone = tmp_path / 'site', tmp_path / 'alone'
    mit(site, 'fine', '1.0', ('License-Expression: MIT',))
    alone.mkdir()
    for place in (site, alone):
        (place / 'legacy-1.0-py3.15.egg-info').write_text('Metadata-Version: 1.1\nName: legacy\nVersion: 1.0\nLicense: GPL\n', encoding='utf-8')
        (place / 'ghost-1.0.dist-info').write_text('Metadata-Version: 2.4\nName: ghost\nVersion: 1.0\nLicense: GPL\n', encoding='utf-8')

    # when
    mixed = check(tmp_path, site, overrides(tmp_path))
    mixed_out = capsys.readouterr().out
    single = check(tmp_path, alone, overrides(tmp_path))
    single_out = capsys.readouterr().out

    # then
    assert (mixed, mixed_out.splitlines()) == (1, ['ghost-1.0.dist-info: unsupported one-file dist-info', 'legacy-1.0-py3.15.egg-info: unsupported one-file egg-info'])
    assert (single, single_out.splitlines()) == (1, ['ghost-1.0.dist-info: unsupported one-file dist-info', 'legacy-1.0-py3.15.egg-info: unsupported one-file egg-info'])


def test_every_file_under_licenses_is_read_as_licence_text(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    bsd = 'Classifier: License :: OSI Approved :: BSD License'
    dist(site, 'named-plus-copying', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE}, unlisted={'licenses/COPYING': GPL_TEXT})
    dist(site, 'nested', '1.0', (bsd,), texts={'LICENSE': THREE_CLAUSE}, unlisted={'licenses/vendor/COPYING': GPL_TEXT})
    unrecorded = dist(site, 'unrecorded', '1.0', (bsd,), unlisted={'licenses/LICENSE': THREE_CLAUSE})
    (unrecorded / 'licenses' / 'COPYING').write_text(GPL_TEXT, encoding='utf-8')
    dist(site, 'clean-extra', '1.0', ('License-Expression: MIT',), texts={'LICENSE': MIT_TEXT}, unlisted={'licenses/NOTICE': 'clean notice\n'})
    dist(site, 'recorded', '1.0', ('License: MIT',), unlisted={'licenses/LICENSE': MIT_TEXT})

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['named-plus-copying 1.0: unknown', 'nested 1.0: unknown', 'unrecorded 1.0: unknown']
    texts = {e.name: e.texts for e in licences.survey(site, licences.Overrides())}
    assert texts['clean-extra'] == (('LICENSE', MIT_TEXT), ('NOTICE', 'clean notice\n'))
    assert texts['nested'] == (('LICENSE', THREE_CLAUSE), ('vendor/COPYING', GPL_TEXT))
    assert texts['recorded'] == (('LICENSE', MIT_TEXT),)


def test_broken_metadata_refuses_the_dist_without_a_traceback(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit(site, 'fine', '1.0', ('License-Expression: MIT',))
    unreadable = mit(site, 'latin-meta', '1.0', ('License-Expression: MIT',))
    (unreadable / 'METADATA').write_bytes(b'Metadata-Version: 2.4\nName: latin-meta\nVersion: 1.0\nAuthor: Ren\xe9\nLicense-Expression: MIT\nLicense-File: LICENSE\n\n')
    nameless = mit(site, 'nameless', '1.0', ('License-Expression: MIT',))
    (nameless / 'METADATA').write_text('Metadata-Version: 2.4\nVersion: 1.0\nLicense-Expression: MIT\nLicense-File: LICENSE\n\n', encoding='utf-8')
    (mit(site, 'missing-meta', '1.0', ('License-Expression: MIT',)) / 'METADATA').unlink()
    mit(site, 'a-copyleft', '1.0', ('License-Expression: GPL-3.0-only',))
    versionless = mit(site, 'versionless', '1.0', ('License-Expression: MIT',))
    (versionless / 'METADATA').write_text('Metadata-Version: 2.4\nName: versionless\nLicense-Expression: MIT\nLicense-File: LICENSE\n\n', encoding='utf-8')

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert (code, capsys.readouterr().out.splitlines()) == (1, ['latin_meta-1.0.dist-info: unreadable metadata', 'missing_meta-1.0.dist-info: no metadata file',
                                                               'nameless-1.0.dist-info: no Name in metadata', 'versionless-1.0.dist-info: no Version in metadata',
                                                               'a-copyleft 1.0: GPL-3.0-only'])
    assert not (tmp_path / 'out' / 'third-party-python.md').exists()


def test_a_licence_field_that_is_not_an_spdx_expression_falls_back_to_the_classifiers(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    dist(site, 'zeta', '1.1', ('License: BSD', 'Classifier: License :: OSI Approved :: Python Software Foundation License'))
    mit(site, 'lazy', '0.4.0', ('License: BSD',))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['lazy 0.4.0: unknown']


def test_unknown_and_copyleft_licences_fail_with_their_names(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    dist(site, 'fine', '1.0', ('License-Expression: BSD-2-Clause',), texts={'LICENSE': TWO_CLAUSE})
    mit(site, 'shared-thing', '4.2.0', ('License-Expression: GPL-3.0-only',))
    mit(site, 'mystery', '0.9', ())
    mit(site, 'mixed', '1.5', ('License-Expression: MIT AND LGPL-2.1-or-later',))
    mit(site, 'either', '2.0', ('License-Expression: (GPL-2.0-only OR MPL-2.0) AND ISC',))
    mit(site, 'excepted', '1.0', ('License-Expression: Apache-2.0 WITH LLVM-exception',))
    mit(site, 'broken-expr', '1.0', ('License-Expression: MIT and-or GPL',))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['broken-expr 1.0: MIT and-or GPL', 'excepted 1.0: Apache-2.0 WITH LLVM-exception', 'mixed 1.5: MIT AND LGPL-2.1-or-later',
                                                    'mystery 0.9: unknown', 'shared-thing 4.2.0: GPL-3.0-only']
    assert not (tmp_path / 'out' / 'third-party-python.md').exists()


def test_an_empty_site_fails_rather_than_listing_nothing(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    site.mkdir()

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert (code, capsys.readouterr().out) == (1, f'no distributions found in {site}\n')


def test_cnri_python_is_allowed_only_for_regex(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    mit(site, 'regex', '2026.11.9', ('License-Expression: Apache-2.0 AND CNRI-Python',))
    mit(site, 'other-regex', '1.0', ('License-Expression: Apache-2.0 AND CNRI-Python',))

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    assert code == 1
    assert capsys.readouterr().out.splitlines() == ['other-regex 1.0: Apache-2.0 AND CNRI-Python']


def test_an_override_needs_a_reason_and_matches_name_and_version(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    dist(site, 'Old_Thing', '1.2', ('License: BSD',), texts={'LICENSE': THREE_CLAUSE})
    mit(site, 'newer', '3.0', ('License: BSD',))
    reviewed = overrides(tmp_path, '[python."old-thing"]\nversion = "1.2"\nlicence = "BSD-3-Clause"\nreason = "LICENSE file is the BSD 3-clause text"\n'
                                    '[python.newer]\nversion = "2.9"\nlicence = "BSD-3-Clause"\nreason = "reviewed for an older release"\n')
    unreasoned = overrides(tmp_path, '[python."old-thing"]\nversion = "1.2"\nlicence = "BSD-3-Clause"\nreason = " "\n', 'unreasoned.toml')

    # when
    first = check(tmp_path, site, reviewed)
    first_out = capsys.readouterr().out
    second = check(tmp_path, site, unreasoned)
    second_out = capsys.readouterr().out

    # then
    assert (first, first_out.splitlines()) == (1, ['newer 3.0: unknown', 'override python.newer 2.9 matches no distribution'])
    assert (second, second_out.splitlines()) == (1, ['override python.old-thing needs a version, a licence and a reason'])


def test_an_override_with_an_unknown_key_or_a_licence_that_is_not_spdx_is_refused(tmp_path, capsys):
    # given
    entry = '[python.old-thing]\nversion = "1.2"\nreason = "read upstream"\n'
    cases = {
        'typo.toml': entry + 'licence = "MIT"\ntxt = "texts/LICENSE"\n',
        'prose.toml': entry + 'licence = "BSD style"\n',
        'debian.toml': '[debian."libsqlite3-0"]\nlicence = "public-domain"\nreason = "copyright file"\n',
        'debian-key.toml': '[debian.libffi8]\nlicence = "MIT"\nreason = "copyright file"\nversion = "3.4"\n',
    }

    # when
    outcomes = {}
    for name, text in cases.items():
        code = licences.main(['debian', '--libs', str(overrides(tmp_path, '', 'libs.txt')), '--out', str(tmp_path / 'out'), '--overrides', str(overrides(tmp_path, text, name))])
        outcomes[name] = (code, capsys.readouterr().out.splitlines())

    # then
    assert outcomes == {
        'typo.toml': (1, ['override python.old-thing has unknown keys: txt']),
        'prose.toml': (1, ['override python.old-thing licence is not an SPDX expression: BSD style']),
        'debian.toml': (1, ['override debian.libsqlite3-0 licence is not an SPDX expression: public-domain']),
        'debian-key.toml': (1, ['override debian.libffi8 has unknown keys: version']),
    }


def test_a_matching_override_is_listed_with_its_reason(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    dist(site, 'Old_Thing', '1.2', ('License: BSD',), texts={'LICENSE': THREE_CLAUSE})
    mit(site, 'dual', '0.9', ('License: MIT License', 'Classifier: License :: OSI Approved :: MIT License', 'Classifier: License :: OSI Approved :: Apache Software License'))
    rules = overrides(tmp_path, '[python."old-thing"]\nversion = "1.2"\nlicence = "BSD-3-Clause"\nreason = "LICENSE file is the BSD 3-clause text"\n'
                                '[python.dual]\nversion = "0.9"\nlicence = "apache-2.0 or mit"\nreason = "offered under either"\n')

    # when
    code = check(tmp_path, site, rules)

    # then
    assert (code, capsys.readouterr().out) == (0, '')
    text = listed(tmp_path)
    assert '| Old_Thing | 1.2 | BSD-3-Clause | reviewed: LICENSE file is the BSD 3-clause text |' in text
    assert '| dual | 0.9 | Apache-2.0 OR MIT | reviewed: offered under either; used under Apache-2.0 |' in text


def test_an_override_can_carry_a_vendored_text_for_a_dist_that_ships_none(tmp_path, capsys):
    # given
    site = tmp_path / 'site'
    dist(site, 'Lazy_Thing', '0.4.0', ('License: BSD',))
    (tmp_path / 'texts').mkdir()
    (tmp_path / 'texts' / 'lazy-thing-0.4.0-LICENSE').write_text('Upstream BSD 3-clause text of lazy-thing\n', encoding='utf-8')
    entry = '[python.lazy-thing]\nversion = "0.4.0"\nlicence = "BSD-3-Clause"\nreason = "upstream LICENSE at the 0.4.0 tag"\n'
    vendored = overrides(tmp_path, entry + 'text = "texts/lazy-thing-0.4.0-LICENSE"\n', 'vendored.toml')
    missing = overrides(tmp_path, entry + 'text = "texts/absent"\n', 'missing.toml')

    # when
    ok = check(tmp_path, site, vendored)
    ok_out = capsys.readouterr().out
    broken = check(tmp_path, site, missing)
    broken_out = capsys.readouterr().out

    # then
    assert (ok, ok_out) == (0, '')
    assert '## Lazy_Thing 0.4.0\n\n### lazy-thing-0.4.0-LICENSE\n\n```text\nUpstream BSD 3-clause text of lazy-thing\n```' in listed(tmp_path)
    assert (broken, broken_out.splitlines()) == (1, ['override python.lazy-thing names a text that is not there: texts/absent'])


def test_the_generated_list_carries_each_licence_text_when_the_dist_ships_one(tmp_path):
    # given
    site = tmp_path / 'site'
    dist(site, 'alpha', '1.0.0', ('License-Expression: MIT',), texts={'LICENSE': 'MIT text of alpha\n'})
    dist(site, 'beta', '2.0', ('License-Expression: Apache-2.0',), texts={'LICENSE': 'Apache text of beta\n', 'NOTICE': 'beta notice ```quoted```\n'})
    dist(site, 'gamma', '0.1', ('License-Expression: ISC',), legacy={'COPYING': 'ISC text of gamma\n'})
    dist(site, 'bare', '5.0', ('License-Expression: Apache-2.0',))
    dist(site, 'older', '0.2', ('License: MIT',), unlisted={'licenses/LICENSE': 'MIT text of older\n', 'NOTICE.txt': 'older notice\n', 'top_level.txt': 'older\n'})

    # when
    code = check(tmp_path, site, overrides(tmp_path))

    # then
    text = listed(tmp_path)
    assert code == 0
    assert text.index('## alpha 1.0.0') < text.index('MIT text of alpha') < text.index('## beta 2.0') < text.index('Apache text of beta') < text.index('beta notice') < text.index('## gamma 0.1') < text.index('ISC text of gamma')
    assert '````text\nbeta notice ```quoted```\n````' in text
    assert '| bare | 5.0 | Apache-2.0 |' in text
    assert text.index('## older 0.2') < text.index('### LICENSE\n\n```text\nMIT text of older') < text.index('### NOTICE.txt\n\n```text\nolder notice')
    assert 'top_level' not in text
    assert '## bare' not in text


def test_debian_libraries_need_a_reviewed_package(tmp_path, capsys):
    # given
    libs = tmp_path / 'libs.txt'
    libs.write_text('/usr/lib/x86_64-linux-gnu/libffi.so.8 libffi8:amd64\n/usr/lib/x86_64-linux-gnu/libfoo.so.1 libfoo1\n/usr/lib/x86_64-linux-gnu/libbar.so.2 libbar2:amd64\n', encoding='utf-8')
    rules = overrides(tmp_path, '[debian.libffi8]\nlicence = "MIT"\nreason = "copyright file: Files * License Expat"\n')
    full = tmp_path / 'full.toml'
    full.write_text(rules.read_text(encoding='utf-8') + '[debian.libfoo1]\nlicence = "BSD-2-Clause"\nreason = "copyright file: Files * License BSD-2-clause"\n'
                    '[debian.libbar2]\nlicence = "0BSD"\nreason = "copyright file: Files * License 0BSD"\n'
                    '[debian.libgone9]\nlicence = "MIT"\nreason = "no longer copied"\n', encoding='utf-8')

    # when
    missing = licences.main(['debian', '--libs', str(libs), '--out', str(tmp_path / 'a'), '--overrides', str(rules)])
    missing_out = capsys.readouterr().out
    reviewed = licences.main(['debian', '--libs', str(libs), '--out', str(tmp_path / 'b'), '--overrides', str(full)])
    reviewed_out = capsys.readouterr().out

    # then
    assert (missing, missing_out.splitlines()) == (1, ['libbar2: no reviewed licence for /usr/lib/x86_64-linux-gnu/libbar.so.2', 'libfoo1: no reviewed licence for /usr/lib/x86_64-linux-gnu/libfoo.so.1'])
    assert not (tmp_path / 'a' / 'third-party-debian.md').exists()
    assert (reviewed, reviewed_out.splitlines()) == (0, ['override debian.libgone9 matches no library'])
    text = (tmp_path / 'b' / 'third-party-debian.md').read_text(encoding='utf-8')
    assert 'The shared libraries this image copies, or relies on the base image for, from Debian packages and the licence reviewed for each package' in text
    assert '| libffi8 | MIT | `/usr/lib/x86_64-linux-gnu/libffi.so.8` | copyright file: Files * License Expat | `/usr/share/doc/libffi8/copyright` |' in text
    assert text.index('| libbar2 |') < text.index('| libffi8 |') < text.index('| libfoo1 |')
    assert 'libgone9' not in text


def test_native_libraries_bundled_in_wheels_need_a_reviewed_licence_and_are_listed(tmp_path, capsys):
    # given
    site = '/usr/local/lib/python3.15/site-packages'
    bundled = overrides(tmp_path, f'{site}/fastmath.libs/libquad-3f2a91c4.so.1.2\n{site}/fastmath.libs/libgfortran-0a1b2c3d.so.5.0.0\n', 'bundled.txt')
    empty = overrides(tmp_path, '', 'empty.txt')
    rules = overrides(tmp_path, '[bundled."fastmath.libs/libquad-3f2a91c4.so.1.2"]\nlicence = "BSD-3-Clause"\nreason = "the wheel ships the libquad BSD text"\n')
    full = overrides(tmp_path, rules.read_text(encoding='utf-8') + '[bundled."fastmath.libs/libgfortran-0a1b2c3d.so.5.0.0"]\nlicence = "GPL-3.0-or-later WITH GCC-exception-3.1"\n'
                     'reason = "runtime library exception"\n[bundled."gone.libs/libold.so"]\nlicence = "MIT"\nreason = "no longer bundled"\n', 'full.toml')
    unreasoned = overrides(tmp_path, '[bundled."fastmath.libs/libquad-3f2a91c4.so.1.2"]\nlicence = "BSD-3-Clause"\nreason = " "\n', 'unreasoned.toml')

    def run(listing: Path, rules: Path, out: str) -> tuple[int, list[str]]:
        code = licences.main(['bundled', '--site', site, '--list', str(listing), '--out', str(tmp_path / out), '--overrides', str(rules)])
        return code, capsys.readouterr().out.splitlines()

    # when
    none = run(empty, overrides(tmp_path, '', 'none.toml'), 'none')
    missing = run(bundled, rules, 'missing')
    reviewed = run(bundled, full, 'reviewed')
    broken = run(bundled, unreasoned, 'broken')

    # then
    assert none == (0, []) and not (tmp_path / 'none').exists()
    assert missing == (1, [f'{site}/fastmath.libs/libgfortran-0a1b2c3d.so.5.0.0: no reviewed licence for a library bundled in a wheel'])
    assert not (tmp_path / 'missing' / 'third-party-bundled.md').exists()
    assert reviewed == (0, ['override bundled.gone.libs/libold.so matches no library'])
    text = (tmp_path / 'reviewed' / 'third-party-bundled.md').read_text(encoding='utf-8')
    assert 'native libraries that Python wheels in this image bundle' in text
    assert '| `fastmath.libs/libgfortran-0a1b2c3d.so.5.0.0` | GPL-3.0-or-later WITH GCC-exception-3.1 | runtime library exception |' in text
    assert text.index('libgfortran') < text.index('| `fastmath.libs/libquad-3f2a91c4.so.1.2` | BSD-3-Clause | the wheel ships the libquad BSD text |')
    assert 'libold' not in text
    assert broken == (1, ['override bundled.fastmath.libs/libquad-3f2a91c4.so.1.2 needs a licence and a reason'])


def test_the_shipped_overrides_carry_a_reason_for_every_entry():
    # when
    rules = licences.load_overrides(licences.OVERRIDES)

    # then
    assert rules.debian and all(entry.reason.strip() for entry in rules.debian.values())
    assert all(entry.reason.strip() for entry in rules.python.values())
    assert 'Neither the name of the copyright holder' in rules.python['lazify'].text_body
    assert (rules.python['uvloop'].version, rules.python['uvloop'].licence) == ('0.23.0', 'Apache-2.0 OR MIT')
    assert {k: rules.debian[k].licence for k in ('libncursesw6', 'libtinfo6', 'libsqlite3-0', 'libzstd1')} == {
        'libncursesw6': 'X11-distribute-modifications-variant', 'libtinfo6': 'X11-distribute-modifications-variant',
        'libsqlite3-0': 'blessing', 'libzstd1': '(BSD-3-Clause OR GPL-2.0-only) AND MIT'}
    assert 'read_termcap.c' in rules.debian['libtinfo6'].reason and 'randutils.c' in rules.debian['libuuid1'].reason
