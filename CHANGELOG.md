# BOSS自动投递 — 问题修复日志

## 2026-06-02

### Git推送配置（中国大陆）
HTTPS 连接 GitHub 会被 reset，需改用 SSH：
```bash
git remote set-url origin git@github.com:bazxhy/Automated-Resume-Submission.git
git push --set-upstream origin main
```

### 问题1：搜索无法触发
**现象**：程序在搜索框填入关键词后不执行搜索，API返回的数据与关键词无关（Java/C++后端岗而非"单片机开发"）。

**根因**：
- `main.py` 使用`tab.get(url)`导航到 `/web/geek/jobs`（复数，错误路由）
- BOSS直聘SPA不会因URL参数自动触发搜索，需模拟用户交互

**解决**：
- URL修正为 `/web/geek/job`（单数，正确路由）
- 新增 `_trigger_search()` 方法：导航→找输入框→填入关键词→按Enter触发搜索
- 找不到输入框时回退到URL导航兜底

### 问题2：API返回数据与页面显示不一致
**现象**：搜索页面上显示正确结果（单片机岗位），但 `_fetch_api` 调API返回推荐页/无关数据（Java/C++岗）。

**根因**：
- BOSS API `/wapi/zpgeek/search/joblist.json` 有反爬机制
- CDP `Runtime.evaluate` + `fetch()` 构造的请求缺少关键headers（如Referer），API返回默认推荐数据而非搜索结果

**尝试过但失败的方法**：
1. DOM解析卡片（能拿到标题但公司名全为"未知公司"）
2. 页面JS状态提取 `extract_from_js_state`（拿到的是推荐页数据）
3. CDP `Page.addScriptToEvaluateOnNewDocument` 注入fetch拦截器（未生效）

**最终解决方案**：
- 使用 DrissionPage 的 `tab.listen` 网络监听
- `start_capture` → `_trigger_search` → `wait_capture`
- 被动监听SPA自己发出的搜索API请求，直接获取响应体
- 翻页同样用监听方式：`start_capture` → 滚动触底 → `wait_capture`

### 问题3：`tab.listen.wait()` 返回类型不一致
**现象**：`'bool' object has no attribute 'response'`

**根因**：DrissionPage的`listen.wait()`可能返回`DataPacket`对象或`True`(bool)

**解决**：`wait_capture()` 同时兼容两种返回：
```python
if resp and not isinstance(resp, bool):
    body = resp.response.body  # DataPacket
elif resp:  # bool=True
    body = tab.listen.steps()[-1].response.body
```

### 问题4：卡片解析性能卡顿
**现象**：`_parse_card`中逐个尝试12个CSS选择器，每个超时1秒，33张卡片×12秒=几分钟卡死

**根因**：对每张卡片调用多次 `card.ele(sel, timeout=1)` 等待不存在的元素

**解决**：改为纯文本行解析，仅在必要时做一次 `card.eles("tag:a")`

### 问题5：翻页数据污染
**现象**：首页捕获正确，但翻页后混入推荐页/无关岗位

**根因**：翻页使用了 `_fetch_api` 自己构造API请求（问题2的延续）

**解决**：翻页统一用listen监听方式，滚动触底→SPA自动发翻页请求→listen捕获

## 当前架构

```
每个关键词:
  1. _trigger_search    → 导航+触发搜索
  2. start_capture      → 启用网络监听
  3. wait_capture       → 捕获首页API响应
  4. 翻页循环:
     start_capture → 滚动触底 → wait_capture
  5. 失败回退: DOM解析
```

数据采集三级策略：
| 优先级 | 方式 | 可靠性 |
|--------|------|--------|
| 1 | listen捕获SPA的API响应 | 最高（页面数据源） |
| 2 | DOM滚动解析 | 中（缺公司名） |
| 3 | CDP fetch构造API请求 | 低（反爬拦截） |
