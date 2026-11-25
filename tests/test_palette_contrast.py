import colorsys
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "skopax" / "web" / "template.html"

HEX = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![\w-])")
RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;}]+)")

SEVERITY_TOKENS = {"--crit", "--crit-bg", "--high", "--high-bg", "--warn", "--warn-bg"}
SERIES_PREFIXES = ("--s-", "--p-")
BRAND_MANDATED = "sso-btn-microsoft"
NEUTRAL_SPREAD = 30
HUE_LOW, HUE_HIGH = 185, 215


def css():
    text = TEMPLATE.read_text(encoding="utf-8")
    return text[text.index("<style>"):text.index("</style>")]


def parse_hex(token):
    h = token[1:]
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def literals(value):
    found = [parse_hex(m.group(0)) for m in HEX.finditer(value)]
    found += [tuple(int(g) for g in m.groups()) for m in RGB.finditer(value)]
    return found


def luminance(rgb):
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def hue(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))[0] * 360


def theme_tokens():
    source = css()
    root = re.search(r":root\s*\{(.*?)\n\}", source, re.S).group(1)
    dark = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\n\}', source, re.S).group(1)
    return dict(DECL.findall(root)), dict(DECL.findall(dark))


def solid(value):
    return literals(value)[0]


def test_body_text_keeps_aa_contrast_in_both_themes():
    # given
    light, dark = theme_tokens()

    # when
    ratios = {}
    for name, theme in (("light", light), ("dark", dark)):
        ratios[name] = (
            contrast(solid(theme["--fg"]), solid(theme["--bg"])),
            contrast(solid(theme["--fg"]), solid(theme["--surface"])),
            contrast(solid(theme["--muted"]), solid(theme["--surface"])),
            contrast(solid(theme["--muted"]), solid(theme["--bg"])),
        )

    # then
    for name, values in ratios.items():
        assert min(values) >= 4.5, (name, values)


def test_accent_and_main_button_keep_aa_contrast_in_both_themes():
    # given
    light, dark = theme_tokens()

    # when
    pairs = [
        contrast(solid(theme["--btn-fg"]), solid(theme["--btn-bg"]))
        for theme in (light, dark)
    ] + [
        contrast(solid(theme["--link"]), solid(theme["--surface"]))
        for theme in (light, dark)
    ]

    # then
    assert min(pairs) >= 4.5, pairs


def test_the_active_fill_is_a_logo_blue_tint():
    # given
    light, dark = theme_tokens()

    # when
    fills = [solid(theme["--accent-soft"]) for theme in (light, dark)]
    accents = [solid(theme["--accent"]) for theme in (light, dark)]

    # then
    for rgb in fills + accents:
        assert HUE_LOW <= hue(rgb) <= HUE_HIGH, rgb


def test_the_chrome_keeps_to_blue_and_neutral():
    # given
    offenders = []

    # when
    for number, line in enumerate(css().split("\n"), 1):
        if BRAND_MANDATED in line:
            continue
        declared = DECL.findall(line)
        rest = DECL.sub("", line)
        checked = [(None, rest)]
        for name, value in declared:
            if name in SEVERITY_TOKENS or name.startswith(SERIES_PREFIXES):
                continue
            checked.append((name, value))
        for name, value in checked:
            for rgb in literals(value):
                if max(rgb) - min(rgb) <= NEUTRAL_SPREAD:
                    continue
                if HUE_LOW <= hue(rgb) <= HUE_HIGH:
                    continue
                offenders.append((number, name, rgb))

    # then
    assert offenders == []


RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
SEVERITY_VAR = re.compile(r"var\(--(?:c|cb)\b")
DOT_SELECTOR = re.compile(r"::before|::after|\.sdot\b")
SEVERITY_EXCEPTIONS = {
    "span.acc-chip": "account role chip, a tone of its own and not a card severity",
    ".acc-table select.acc-chip": "account role select, same chip",
    ".adm-notice": "admin notice keeps its left edge in the notice tone",
    ".banner": "banner keeps its left edge in the banner tone",
    "#svcs tr.clk:hover td, #svcs tr.clk:focus-within td": "row hover fill",
    ".cmeter": "usage meter track and bar",
    ".cmeter > span": "usage meter bar",
    '.acts.acts-grouped .seg > .btn[aria-pressed="true"]': "pressed segment sets its own --c from --info",
    ".k-crit": "scope definitions of the severity vars",
}
EDGE_PROPERTIES = ("border-left", "border-left-color", "box-shadow")


def rules():
    for selector, body in RULE.findall(css()):
        selector = selector.strip()
        if selector.startswith("@") or not selector:
            continue
        yield " ".join(selector.split()), body


def split_declarations(body):
    return [d.split(":", 1) for d in re.split(r";(?![^()]*\))", body) if ":" in d]


def test_severity_colour_only_marks_dots_and_edges():
    # given
    offenders = []

    # when
    for selector, body in rules():
        if selector in SEVERITY_EXCEPTIONS or selector.startswith(".k-"):
            continue
        for prop, value in split_declarations(body):
            prop = prop.strip()
            if not SEVERITY_VAR.search(value) or prop.startswith("--"):
                continue
            if prop in EDGE_PROPERTIES:
                continue
            if prop == "background" and DOT_SELECTOR.search(selector):
                continue
            offenders.append((selector, prop, value.strip()[:60]))

    # then
    assert offenders == [], offenders


def test_tag_text_uses_no_severity_colour():
    # given
    tag_rules = [(s, b) for s, b in rules() if re.search(r"(^|[\s,])\.tag(\.[\w-]+)*(\s|$|,)", s) and "var(--c" in b]

    # when
    texts = [
        (s, v.strip())
        for s, b in tag_rules
        for p, v in split_declarations(b)
        if p.strip() == "color" and "var(--c" in v
    ]

    # then
    assert texts == [], texts


def blend(over, under, alpha):
    return tuple(round(o * alpha + u * (1 - alpha)) for o, u in zip(over, under))


def side_theme(selector_pattern):
    source = css()
    body = re.search(selector_pattern + r"\s*\{(.*?)\n\s*\}", source, re.S).group(1)
    return dict(DECL.findall(body))


def side_colour(value, base):
    m = RGB.search(value)
    if m and "rgba" in value:
        alpha = float(re.search(r",\s*([\d.]+)\s*\)", value).group(1))
        return blend(tuple(int(g) for g in m.groups()), base, alpha)
    return solid(value)


def test_the_sidebar_text_keeps_aa_contrast_in_both_themes():
    # given
    light_root, dark_root = theme_tokens()
    light_side = side_theme(r"\n\.side")
    dark_side = side_theme(r':root\[data-theme="dark"\] \.side')
    cases = (
        ("light", {**light_root, **light_side}, solid(light_root["--bar-bg"])),
        ("dark", {**dark_root, **dark_side}, solid(dark_root["--bar-bg"])),
    )

    # when
    ratios = {}
    for name, tokens, bar in cases:
        fg = side_colour(tokens["--fg"], bar)
        fg2 = side_colour(tokens["--fg-2"], bar)
        muted = side_colour(tokens["--muted"], bar)
        surface = blend(side_colour(tokens["--surface"], bar), bar, 1.0) if "rgba" not in tokens["--surface"] else blend(solid(tokens["--surface"]), bar, 0.045)
        ratios[name] = (contrast(fg, bar), contrast(fg2, bar), contrast(muted, bar), contrast(muted, surface))

    # then
    for name, values in ratios.items():
        assert min(values) >= 4.5, (name, values)


def test_the_light_sidebar_is_light_and_the_dark_one_stays_dark():
    # given
    light_root, dark_root = theme_tokens()

    # when
    light_bar, dark_bar = solid(light_root["--bar-bg"]), solid(dark_root["--bar-bg"])

    # then
    assert luminance(light_bar) > 0.7
    assert luminance(dark_bar) < 0.05
