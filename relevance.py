"""Shared keyword/skill matching with Latin token boundaries and explicit aliases."""
import re
import unicodedata


def normalize(text):
    return unicodedata.normalize('NFKC', str(text or '')).casefold().strip()


def contains_term(text, term):
    text, term = normalize(text), normalize(term)
    if not term:
        return False
    if re.search(r'[a-z0-9]', term):
        return bool(re.search(r'(?<![a-z0-9+#])' + re.escape(term) + r'(?![a-z0-9+#])', text))
    return term in text


ALIASES = (
    ('电商运营', '电子商务运营', '网店运营', '店铺运营'),
    ('京东', '京东自营'), ('淘宝', '淘系'),
    ('商品上架', '商品上下架', '产品上架'),
    ('标题优化', '标题与卖点打磨'),
    ('主图优化', '主图设计', '主图ab测试'),
    ('竞品分析', '竞品调研'), ('数据分析', '数据复盘'),
    ('生意参谋',), ('京东商智', '商智'), ('京东快车',),
    ('直通车',), ('万相台',), ('引力魔方',),
    ('详情页', '详情优化'), ('活动报名', '活动运营'),
    ('天猫',), ('苏宁',), ('拼多多',), ('抖店', '抖音小店'),
    ('photoshop', 'ps'), ('剪映',),
    ('llm', '大模型', '大语言模型', 'large language model'),
    ('agent', '智能体'), ('rag', '检索增强'),
    ('aigc', '生成式ai', '生成式 ai'),
    ('golang', 'go'), ('kubernetes', 'k8s'),
    ('pytorch', 'torch'), ('单片机', 'mcu'),
)

ECOMMERCE_SKILLS = (
    '电商运营', '京东', '淘宝', '天猫', '苏宁', '拼多多', '抖店',
    '商品上架', '标题优化', '主图优化', '竞品分析', '数据分析',
    '生意参谋', '京东商智', '京东快车', '直通车', '万相台', '引力魔方',
    '详情页', '活动报名', 'photoshop', '剪映',
)


def extract_ecommerce_skills(text):
    return {group[0] for group in ALIASES if group[0] in ECOMMERCE_SKILLS
            and any(contains_term(text, alias) for alias in group)}
GENERIC = ('工程师', '工程', '实习生', '实习', '开发', '应用', '高级', '初级', '中级',
           '校招', '应届生', '应届', '招聘', '技术', '专员', '助理')


def terms_for_keyword(keyword):
    text = normalize(keyword)
    groups = [group for group in ALIASES if any(contains_term(text, t) for t in group)]
    for group in groups:
        for term in group:
            text = text.replace(term, ' ')
    for word in GENERIC:
        text = text.replace(word, ' ')
    terms = re.findall(r'[a-z][a-z0-9+#.]*|[\u4e00-\u9fff]{2,}', text)
    return groups + [(term,) for term in terms]


def keyword_matches(title, keyword, description='', skills=()):
    """Require substantive terms; generic words like 实习/开发 cannot match everything."""
    groups = terms_for_keyword(keyword)
    if not groups:
        return contains_term(title, keyword)
    title = normalize(title)
    # An explicit direction in the title is enough; otherwise use actual JD/skills.
    if all(any(contains_term(title, t) for t in group) for group in groups):
        return True
    # Detailed JD takes priority over noisy recommendation labels.
    detail = str(description or '') if len(str(description or '').strip()) >= 30 else ' '.join([str(description or ''), *map(str, skills or ())])
    combined = title + ' ' + detail
    return bool(detail.strip()) and all(
        any(contains_term(combined, t) for t in group) for group in groups)


def canonical_skill(skill):
    value = normalize(skill)
    for group in ALIASES:
        if value in group:
            return group[0]
    return value


def is_skill_label(value):
    """Salary, degree, experience and attendance labels are not job skills."""
    value = normalize(value).replace(' ', '')
    return bool(value) and not re.fullmatch(
        r'(?:全日制)?(?:本科|大专|专科|硕士|博士|高中|中专|学士)(?:及以上|以上)?'
        r'|(?:学历|经验)?不限|在校(?:/应届)?|应届(?:生)?|实习(?:生)?'
        r'|\d+(?:[-~—–]\d+)?年(?:以上|以内)?|\d+个?月(?:以上)?'
        r'|\d+天/[周月]|\d+小时|\d+(?:[-~]\d+)?[kK](?:.*)?', value)
