#!/usr/bin/env python3
import hmac
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

BASE_DIR = Path(os.environ.get("TRENDRADAR_BASE_DIR", "/app"))
CONFIG_DIR = Path(os.environ.get("TRENDRADAR_CONFIG_DIR", str(BASE_DIR / "config")))
OUTPUT_DIR = Path(os.environ.get("TRENDRADAR_OUTPUT_DIR", str(BASE_DIR / "output")))
DOCS_DIR = Path(os.environ.get("TRENDRADAR_DOCS_DIR", str(BASE_DIR / "docs")))
ADMIN_DIR = Path(os.environ.get("TRENDRADAR_ADMIN_DIR", str(BASE_DIR / "admin")))
BACKUP_DIR = CONFIG_DIR / "backups"
ADMIN_TOKEN = os.environ.get("TRENDRADAR_ADMIN_TOKEN", "").strip()
if not ADMIN_TOKEN and os.environ.get("TRENDRADAR_ADMIN_TOKEN_FILE"):
    try:
        ADMIN_TOKEN = Path(os.environ["TRENDRADAR_ADMIN_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    except OSError:
        ADMIN_TOKEN = ""
RUN_LOCK = threading.Lock()
RUN_STATE = {"running": False, "started_at": None, "finished_at": None, "exit_code": None, "log": ""}

TEXT_FILES = {
    "config": {"path": CONFIG_DIR / "config.yaml", "label": "主配置 config.yaml", "kind": "yaml"},
    "frequency": {"path": CONFIG_DIR / "frequency_words.txt", "label": "关键词 frequency_words.txt", "kind": "text"},
    "timeline": {"path": CONFIG_DIR / "timeline.yaml", "label": "时间线 timeline.yaml", "kind": "yaml"},
    "ai_interests": {"path": CONFIG_DIR / "ai_interests.txt", "label": "AI 兴趣 ai_interests.txt", "kind": "text"},
    "ai_analysis_prompt": {"path": CONFIG_DIR / "ai_analysis_prompt.txt", "label": "AI 分析提示词", "kind": "text"},
    "ai_translation_prompt": {"path": CONFIG_DIR / "ai_translation_prompt.txt", "label": "AI 翻译提示词", "kind": "text"},
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def validate_file(file_key: str, content: str):
    spec = TEXT_FILES[file_key]
    if spec["kind"] != "yaml":
        return True
    loaded = yaml.safe_load(content) if content.strip() else {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{spec['label']} 必须是 YAML 对象")
    if file_key == "config":
        required = ("app", "platforms", "rss", "report", "filter", "notification")
        missing = [key for key in required if key not in loaded]
        if missing:
            raise ValueError("config.yaml 缺少必要段落: " + ", ".join(missing))
    if file_key == "timeline" and "presets" not in loaded and "custom" not in loaded:
        raise ValueError("timeline.yaml 至少需要 presets 或 custom 段落")
    return True


def backup_file(path: Path, reason="save"):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUP_DIR / f"{stamp}-{reason}-{path.name}"
    shutil.copy2(path, dest)
    return dest.name


def load_yaml_file(file_key):
    content = read_text(TEXT_FILES[file_key]["path"])
    return (yaml.safe_load(content) or {}, content) if content.strip() else ({}, content)


def dump_yaml(data):
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)


def set_nested(data, dotted, value):
    current = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def latest_report():
    candidates = [OUTPUT_DIR / "html" / "latest" / "current.html", OUTPUT_DIR / "index.html"]
    for path in candidates:
        if path.exists():
            return {"path": str(path), "mtime": path.stat().st_mtime, "size": path.stat().st_size}
    return None


def service_urls(handler):
    scheme = handler.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip() or "http"
    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "127.0.0.1:8081"
    admin_url = os.environ.get("TRENDRADAR_ADMIN_PUBLIC_URL", "").strip() or f"{scheme}://{host}/"
    web_url = os.environ.get("TRENDRADAR_WEB_PUBLIC_URL", "").strip()
    if not web_url:
        web_port = os.environ.get("TRENDRADAR_WEB_PORT", "8080")
        hostname = host
        if host.startswith("[") and "]" in host:
            hostname = host[:host.index("]") + 1]
        elif ":" in host:
            hostname = host.split(":", 1)[0]
        web_url = f"{scheme}://{hostname}:{web_port}/"
    return {"web": web_url.rstrip("/") + "/", "admin": admin_url.rstrip("/") + "/"}


def list_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(BACKUP_DIR.glob("*"), reverse=True)[:100]:
        if path.is_file():
            items.append({"name": path.name, "size": path.stat().st_size, "mtime": path.stat().st_mtime})
    return items


def safe_child(base: Path, request_path: str):
    target = (base / request_path.lstrip("/")).resolve()
    base_resolved = base.resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError:
        return None
    return target


def content_type_for(path: Path):
    guess, _ = mimetypes.guess_type(str(path))
    if guess:
        if guess.startswith("text/") or guess in ("application/javascript", "application/json"):
            return guess + "; charset=utf-8"
        return guess
    return "application/octet-stream"


def official_editor_html():
    index_path = DOCS_DIR / "index.html"
    if not index_path.exists():
        return "<!doctype html><meta charset=\"utf-8\"><title>TrendRadar Admin</title><p>官方配置编辑器文件不存在，请检查 /app/docs/index.html。</p>"
    html = index_path.read_text(encoding="utf-8")
    html = html.replace("纯静态页面，数据仅保存在你的本地浏览器，请放心使用", "服务器增强模式：配置保存到当前 TrendRadar 部署")
    bridge = '    <script src="/admin-bridge.js"></script>\n'
    if "/admin-bridge.js" not in html:
        html = html.replace("</body>", bridge + "</body>")
    html = remove_official_header_actions(html)
    return remove_support_sidebar(html)


def remove_official_header_actions(html: str) -> str:
    marker = '<div class="flex gap-3">'
    search_from = 0
    while True:
        start = html.find(marker, search_from)
        if start < 0:
            return html

        tag_pattern = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)
        depth = 0
        for match in tag_pattern.finditer(html, start):
            tag = match.group(0)
            if tag.startswith("</"):
                depth -= 1
                if depth == 0:
                    block = html[start:match.end()]
                    if "加载官网最新配置" in block and "复制配置" in block:
                        return html[:start] + html[match.end():]
                    search_from = match.end()
                    break
            else:
                depth += 1
        else:
            return html


def remove_support_sidebar(html: str) -> str:
    marker = '<div class="support-sidebar-wrap'
    start = html.find(marker)
    if start < 0:
        return html

    tag_pattern = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)
    depth = 0
    for match in tag_pattern.finditer(html, start):
        tag = match.group(0)
        if tag.startswith("</"):
            depth -= 1
            if depth == 0:
                return html[:start] + html[match.end():]
        else:
            depth += 1
    return html


def run_crawl_background():
    with RUN_LOCK:
        if RUN_STATE.get("running"):
            return False
        RUN_STATE.update({"running": True, "started_at": now_iso(), "finished_at": None, "exit_code": None, "log": ""})

    def target():
        try:
            proc = subprocess.run(
                ["python", "-m", "trendradar"],
                cwd=str(BASE_DIR),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=int(os.environ.get("TRENDRADAR_RUN_TIMEOUT", "900")),
            )
            with RUN_LOCK:
                RUN_STATE["exit_code"] = proc.returncode
                RUN_STATE["log"] = proc.stdout[-50000:]
        except Exception as exc:
            with RUN_LOCK:
                RUN_STATE["exit_code"] = -1
                RUN_STATE["log"] = str(exc)
        finally:
            with RUN_LOCK:
                RUN_STATE["finished_at"] = now_iso()
                RUN_STATE["running"] = False

    threading.Thread(target=target, daemon=True).start()
    return True


HTML_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TrendRadar Admin</title>
<style>
:root{--bg:#f6f7fb;--card:#fff;--line:#e5e7eb;--text:#111827;--muted:#6b7280;--brand:#4f46e5;--ok:#059669;--bad:#dc2626}*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text)}header{padding:24px 28px;background:linear-gradient(135deg,#312e81,#7c3aed);color:white}header h1{margin:0 0 6px;font-size:26px}header p{margin:0;color:#ddd6fe}main{max-width:1260px;margin:0 auto;padding:22px}.grid{display:grid;grid-template-columns:250px 1fr;gap:18px}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:0 8px 28px rgba(15,23,42,.05)}nav{padding:10px}.tab{display:block;width:100%;border:0;background:transparent;text-align:left;padding:12px 14px;border-radius:12px;cursor:pointer;color:#374151;font-size:14px}.tab:hover,.tab.active{background:#eef2ff;color:#3730a3;font-weight:700}.section{display:none;padding:20px}.section.active{display:block}h2{margin:0 0 16px;font-size:20px}h3{margin:22px 0 10px}.muted{color:var(--muted);font-size:13px}.row{display:grid;grid-template-columns:190px 1fr;gap:12px;align-items:center;margin:10px 0}.row label{font-size:14px;color:#374151}.row input,.row select,.row textarea{width:100%;border:1px solid var(--line);border-radius:10px;padding:10px;font-size:14px;background:white}.btn{border:0;border-radius:10px;padding:10px 14px;background:#e5e7eb;cursor:pointer;font-weight:700;text-decoration:none;color:#111827;display:inline-block}.btn.primary{background:var(--brand);color:white}.btn.ok{background:var(--ok);color:white}.btn.bad{background:var(--bad);color:white}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}textarea.editor{width:100%;min-height:520px;border:1px solid var(--line);border-radius:12px;padding:12px;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px;line-height:1.45}.pill{display:inline-flex;padding:4px 8px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;margin:2px}.status{white-space:pre-wrap;background:#0f172a;color:#e5e7eb;padding:14px;border-radius:12px;max-height:440px;overflow:auto;font-family:ui-monospace,Consolas,monospace;font-size:12px}.toast{position:fixed;right:20px;bottom:20px;background:#111827;color:white;padding:12px 16px;border-radius:12px;display:none;box-shadow:0 10px 30px rgba(0,0,0,.25)}.table{width:100%;border-collapse:collapse}.table th,.table td{border-bottom:1px solid var(--line);padding:10px;text-align:left;font-size:13px}.table input{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px}.hint{padding:12px;border-radius:12px;background:#f8fafc;border:1px solid var(--line);color:#475569}@media(max-width:820px){.grid{grid-template-columns:1fr}.row{grid-template-columns:1fr}}
</style>
</head>
<body><header><h1>TrendRadar Admin</h1><p>保存配置会校验、备份、原子写入；下一次定时采集自动生效，也可点击立即运行。</p></header><main><div class="grid"><nav class="card" id="nav"></nav><div class="card">
<section id="dashboard" class="section active"><h2>状态总览</h2><div id="summary" class="hint">加载中...</div><div class="actions"><button class="btn primary" onclick="runNow()">立即运行一次</button><a class="btn" href="/report" target="_blank">打开最新报告</a><button class="btn" onclick="loadAll()">刷新</button></div><h3>运行日志</h3><pre id="runlog" class="status">暂无</pre></section>
<section id="basic" class="section"><h2>基础设置</h2><div id="basicForm"></div><div class="actions"><button class="btn primary" onclick="saveBasic()">保存基础设置</button></div></section>
<section id="platforms" class="section"><h2>热榜平台</h2><div class="hint">来自 config.yaml 的 platforms.sources；保存后下一次采集生效。</div><div id="platformList"></div><div class="actions"><button class="btn primary" onclick="savePlatforms()">保存平台设置</button></div></section>
<section id="rss" class="section"><h2>RSS 订阅</h2><div class="row"><label>启用 RSS</label><input id="rss_enabled" type="checkbox"></div><div class="row"><label>新鲜度过滤/天</label><input id="rss_max_age_days" type="number" min="0"></div><table class="table"><thead><tr><th>启用</th><th>ID</th><th>名称</th><th>URL</th><th></th></tr></thead><tbody id="rssRows"></tbody></table><div class="actions"><button class="btn" onclick="addRssRow()">添加 RSS</button><button class="btn primary" onclick="saveRss()">保存 RSS</button></div></section>
<section id="keywords" class="section"><h2>关键词配置</h2><textarea id="frequencyText" class="editor"></textarea><div class="actions"><button class="btn primary" onclick="saveTextFile('frequency')">保存关键词</button></div></section>
<section id="ai" class="section"><h2>AI 设置</h2><div id="aiForm"></div><h3>AI 兴趣描述</h3><textarea id="aiInterests" class="editor" style="min-height:220px"></textarea><div class="actions"><button class="btn primary" onclick="saveAi()">保存 AI 设置</button></div></section>
<section id="notify" class="section"><h2>通知渠道</h2><div class="hint">密码/Token 输入框留空表示不覆盖现有值。</div><div id="notifyForm"></div><div class="actions"><button class="btn primary" onclick="saveNotify()">保存通知设置</button></div></section>
<section id="timeline" class="section"><h2>时间线 timeline.yaml</h2><textarea id="timelineText" class="editor"></textarea><div class="actions"><button class="btn primary" onclick="saveTextFile('timeline')">保存时间线</button></div></section>
<section id="prompts" class="section"><h2>提示词文件</h2><h3>AI 分析提示词</h3><textarea id="aiAnalysisPrompt" class="editor" style="min-height:220px"></textarea><h3>AI 翻译提示词</h3><textarea id="aiTranslationPrompt" class="editor" style="min-height:220px"></textarea><div class="actions"><button class="btn primary" onclick="saveTextFile('ai_analysis_prompt');saveTextFile('ai_translation_prompt')">保存提示词</button></div></section>
<section id="raw" class="section"><h2>原始 config.yaml</h2><textarea id="configText" class="editor"></textarea><div class="actions"><button class="btn primary" onclick="saveTextFile('config')">保存 config.yaml</button><button class="btn" onclick="validateRaw()">校验</button></div></section>
<section id="backups" class="section"><h2>备份与回滚</h2><div id="backupList"></div></section>
</div></div></main><div id="toast" class="toast"></div>
<script>
const tabs=[['dashboard','状态'],['basic','基础设置'],['platforms','热榜平台'],['rss','RSS'],['keywords','关键词'],['ai','AI'],['notify','通知'],['timeline','时间线'],['prompts','提示词'],['raw','原始配置'],['backups','备份']];let state={};
function toast(msg){const el=document.getElementById('toast');el.textContent=msg;el.style.display='block';setTimeout(()=>el.style.display='none',3600)}
async function api(path,opts={}){const r=await fetch('/api'+path,{headers:{'Content-Type':'application/json'},...opts});const t=await r.text();let j;try{j=JSON.parse(t)}catch{j={success:false,error:t}}if(!r.ok||j.success===false)throw new Error(j.error||j.message||r.statusText);return j}
function initNav(){nav.innerHTML=tabs.map(([id,n])=>`<button class="tab ${id==='dashboard'?'active':''}" onclick="showTab('${id}',this)">${n}</button>`).join('')}
function showTab(id,btn){document.querySelectorAll('.section').forEach(x=>x.classList.remove('active'));document.getElementById(id).classList.add('active');document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));btn.classList.add('active');if(id==='backups')loadBackups()}
function val(id){const el=document.getElementById(id);return el.type==='checkbox'?el.checked:el.value}function setv(id,v){const el=document.getElementById(id);if(!el)return;if(el.type==='checkbox')el.checked=!!v;else el.value=v??''}function get(obj,path,d=''){const v=path.split('.').reduce((a,k)=>a&&a[k]!==undefined?a[k]:undefined,obj);return v===undefined?d:v}function esc(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}function field(id,label,type='text'){return `<div class="row"><label>${label}</label><input id="${id}" type="${type}"></div>`}
async function loadAll(){try{state=await api('/state');renderAll();toast('已加载')}catch(e){toast(e.message)}}
function renderAll(){const c=state.config||{};summary.innerHTML=`<b>Web:</b> <a href="${state.service.web}" target="_blank">${state.service.web}</a><br><b>Admin:</b> ${state.service.admin}<br><b>最新报告:</b> ${state.report?new Date(state.report.mtime*1000).toLocaleString():'无'}<br><b>配置文件:</b> ${(state.files||[]).map(f=>`<span class="pill">${f.label}</span>`).join('')}`;configText.value=state.texts.config||'';frequencyText.value=state.texts.frequency||'';timelineText.value=state.texts.timeline||'';aiInterests.value=state.texts.ai_interests||'';aiAnalysisPrompt.value=state.texts.ai_analysis_prompt||'';aiTranslationPrompt.value=state.texts.ai_translation_prompt||'';renderBasic(c);renderPlatforms(c);renderRss(c);renderAi(c);renderNotify();loadStatus()}
function renderBasic(c){basicForm.innerHTML=field('app_timezone','时区')+field('report_mode','报告模式 current/daily')+field('schedule_enabled','启用时间线调度','checkbox')+field('schedule_preset','调度 preset')+field('filter_method','筛选方式 keyword/ai')+field('rank_threshold','排名阈值','number')+field('max_news_per_keyword','每关键词最大条数','number')+field('notification_enabled','启用通知','checkbox');setv('app_timezone',get(c,'app.timezone','Asia/Shanghai'));setv('report_mode',get(c,'report.mode','current'));setv('schedule_enabled',get(c,'schedule.enabled',false));setv('schedule_preset',get(c,'schedule.preset','always_on'));setv('filter_method',get(c,'filter.method','keyword'));setv('rank_threshold',get(c,'report.rank_threshold',10));setv('max_news_per_keyword',get(c,'report.max_news_per_keyword',0));setv('notification_enabled',get(c,'notification.enabled',true))}
function renderPlatforms(c){const arr=get(c,'platforms.sources',[]);platformList.innerHTML=arr.map((p,i)=>`<div class="row"><label><input type="checkbox" id="pf_en_${i}" ${p.enabled!==false?'checked':''}> ${esc(p.id)}</label><input id="pf_name_${i}" value="${esc(p.name||'')}"><input type="hidden" id="pf_id_${i}" value="${esc(p.id)}"><input type="hidden" id="pf_domain_${i}" value="${esc(p.expected_domain||'')}"></div>`).join('')}
function renderRss(c){setv('rss_enabled',get(c,'rss.enabled',false));setv('rss_max_age_days',get(c,'rss.freshness_filter.max_age_days',3));rssRows.innerHTML='';(get(c,'rss.feeds',[])||[]).forEach(addRssRow)}
function addRssRow(feed={}){const i=rssRows.children.length;rssRows.insertAdjacentHTML('beforeend',`<tr><td><input type="checkbox" id="rss_en_${i}" ${feed.enabled!==false?'checked':''}></td><td><input id="rss_id_${i}" value="${esc(feed.id||'')}"></td><td><input id="rss_name_${i}" value="${esc(feed.name||'')}"></td><td><input id="rss_url_${i}" value="${esc(feed.url||'')}"></td><td><button class="btn bad" onclick="this.closest('tr').remove()">删</button></td></tr>`)}
function renderAi(c){aiForm.innerHTML=field('ai_filter_min_score','AI 筛选最低分','number')+field('ai_batch_size','AI 批量大小','number')+field('ai_analysis_enabled','启用 AI 分析','checkbox')+field('ai_translation_enabled','启用 AI 翻译','checkbox')+field('ai_translation_target','翻译目标语言')+field('ai_api_base','AI API Base')+field('ai_model','AI 模型')+field('ai_api_key','AI API Key','password');setv('ai_filter_min_score',get(c,'ai_filter.min_score',0.7));setv('ai_batch_size',get(c,'ai_filter.batch_size',200));setv('ai_analysis_enabled',get(c,'ai_analysis.enabled',false));setv('ai_translation_enabled',get(c,'ai_translation.enabled',false));setv('ai_translation_target',get(c,'ai_translation.target_language','中文'));setv('ai_api_base',get(c,'ai.api_base',''));setv('ai_model',get(c,'ai.model',''));setv('ai_api_key','')}
function renderNotify(){notifyForm.innerHTML=field('feishu_webhook','飞书 Webhook','password')+field('telegram_bot_token','Telegram Bot Token','password')+field('telegram_chat_id','Telegram Chat ID')+field('dingtalk_webhook','钉钉 Webhook','password')+field('wework_webhook','企业微信 Webhook','password')+field('email_from','发件邮箱')+field('email_password','邮箱密码','password')+field('email_to','收件邮箱')+field('ntfy_topic','ntfy Topic')+field('bark_url','Bark URL','password')+field('slack_webhook','Slack Webhook','password')+field('generic_webhook','通用 Webhook','password')}
async function saveTextFile(key){const ids={config:'configText',frequency:'frequencyText',timeline:'timelineText',ai_interests:'aiInterests',ai_analysis_prompt:'aiAnalysisPrompt',ai_translation_prompt:'aiTranslationPrompt'};try{await api('/file/'+key,{method:'POST',body:JSON.stringify({content:document.getElementById(ids[key]).value})});toast('已保存并备份 '+key);await loadAll()}catch(e){toast(e.message)}}
async function savePatch(patch){await api('/config/patch',{method:'POST',body:JSON.stringify({patch})});toast('已保存配置');await loadAll()}
function saveBasic(){savePatch({'app.timezone':val('app_timezone'),'report.mode':val('report_mode'),'schedule.enabled':val('schedule_enabled'),'schedule.preset':val('schedule_preset'),'filter.method':val('filter_method'),'report.rank_threshold':Number(val('rank_threshold')),'report.max_news_per_keyword':Number(val('max_news_per_keyword')),'notification.enabled':val('notification_enabled')})}
function savePlatforms(){const sources=[];document.querySelectorAll('[id^=pf_id_]').forEach((el,i)=>sources.push({id:el.value,name:val('pf_name_'+i),expected_domain:val('pf_domain_'+i),enabled:val('pf_en_'+i)}));savePatch({'platforms.sources':sources})}
function saveRss(){const feeds=[];[...rssRows.children].forEach((tr,i)=>{const id=val('rss_id_'+i),url=val('rss_url_'+i);if(id&&url)feeds.push({id,name:val('rss_name_'+i),url,enabled:val('rss_en_'+i)})});savePatch({'rss.enabled':val('rss_enabled'),'rss.freshness_filter.max_age_days':Number(val('rss_max_age_days')),'rss.feeds':feeds})}
async function saveAi(){const patch={'ai_filter.min_score':Number(val('ai_filter_min_score')),'ai_filter.batch_size':Number(val('ai_batch_size')),'ai_analysis.enabled':val('ai_analysis_enabled'),'ai_translation.enabled':val('ai_translation_enabled'),'ai_translation.target_language':val('ai_translation_target'),'ai.api_base':val('ai_api_base'),'ai.model':val('ai_model')};if(val('ai_api_key'))patch['ai.api_key']=val('ai_api_key');await savePatch(patch);await saveTextFile('ai_interests')}
function saveNotify(){const p={};[['notification.feishu.webhook_url','feishu_webhook'],['notification.telegram.bot_token','telegram_bot_token'],['notification.telegram.chat_id','telegram_chat_id'],['notification.dingtalk.webhook_url','dingtalk_webhook'],['notification.wework.webhook_url','wework_webhook'],['notification.email.from','email_from'],['notification.email.password','email_password'],['notification.email.to','email_to'],['notification.ntfy.topic','ntfy_topic'],['notification.bark.url','bark_url'],['notification.slack.webhook_url','slack_webhook'],['notification.generic_webhook.url','generic_webhook']].forEach(([k,id])=>{if(val(id))p[k]=val(id)});savePatch(p)}
async function validateRaw(){try{await api('/validate',{method:'POST',body:JSON.stringify({file:'config',content:configText.value})});toast('校验通过')}catch(e){toast(e.message)}}
async function runNow(){try{await api('/run',{method:'POST',body:'{}'});toast('已开始运行');setTimeout(loadStatus,1500)}catch(e){toast(e.message)}}
async function loadStatus(){try{const s=await api('/run');runlog.textContent=(s.running?'运行中...\n':'')+(s.log||'暂无手动运行日志')}catch(e){runlog.textContent=e.message}}
async function loadBackups(){try{const b=await api('/backups');backupList.innerHTML='<table class="table"><tr><th>文件</th><th>大小</th><th>时间</th><th></th></tr>'+b.items.map(x=>`<tr><td>${esc(x.name)}</td><td>${x.size}</td><td>${new Date(x.mtime*1000).toLocaleString()}</td><td><button class="btn" onclick="restoreBackup('${esc(x.name)}')">回滚</button></td></tr>`).join('')+'</table>'}catch(e){toast(e.message)}}
async function restoreBackup(name){if(!confirm('确认回滚 '+name+' ?'))return;try{await api('/backup/restore',{method:'POST',body:JSON.stringify({name})});toast('已回滚');await loadAll();await loadBackups()}catch(e){toast(e.message)}}
setInterval(loadStatus,5000);initNav();loadAll();
</script></body></html>'''

LOGIN_PAGE = '''<!doctype html><html><head><meta charset="utf-8"><title>TrendRadar Admin Login</title><style>body{font-family:sans-serif;background:#f6f7fb;display:grid;place-items:center;height:100vh}.box{background:white;padding:28px;border-radius:16px;box-shadow:0 10px 30px #0001}input{padding:10px;border:1px solid #ddd;border-radius:8px;width:280px}button{padding:10px 14px;border:0;border-radius:8px;background:#4f46e5;color:white;font-weight:700}</style></head><body><form class="box" method="GET"><h2>TrendRadar Admin</h2><p>请输入访问 Token</p><input name="token" type="password" autofocus><button>进入</button></form></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "TrendRadarAdmin/1.0"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def _token_from_cookie(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            if part.strip().startswith("tr_admin_token="):
                return part.strip().split("=", 1)[1]
        return ""

    def _authorized(self):
        if not ADMIN_TOKEN:
            return False
        auth_value = self.headers.get("Authorization", "")
        return hmac.compare_digest(auth_value, f"Bearer {ADMIN_TOKEN}") or hmac.compare_digest(self._token_from_cookie(), ADMIN_TOKEN)

    def _send(self, status=200, body=b"", content_type="application/json; charset=utf-8", extra_headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_HEAD(self):
        self._send(200, b"", "text/plain; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if ADMIN_TOKEN and hmac.compare_digest(query.get("token", [""])[0], ADMIN_TOKEN):
            self._send(302, "", "text/plain", {"Set-Cookie": f"tr_admin_token={ADMIN_TOKEN}; Path=/; SameSite=Lax; HttpOnly", "Location": "/"})
            return
        if parsed.path == "/" and not self._authorized():
            self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
            return
        if parsed.path in ("/admin-bridge.js", "/report") and not self._authorized():
            self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/assets/") and not self._authorized():
            self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/api") and not self._authorized():
            self._json({"success": False, "error": "unauthorized"}, 401)
            return
        try:
            if parsed.path == "/":
                self._send(200, official_editor_html(), "text/html; charset=utf-8")
            elif parsed.path == "/simple":
                self._send(404, "simple admin removed", "text/plain; charset=utf-8")
            elif parsed.path == "/admin-bridge.js":
                bridge_path = ADMIN_DIR / "admin_bridge.js"
                if bridge_path.exists():
                    self._send(200, bridge_path.read_bytes(), "application/javascript; charset=utf-8")
                else:
                    self._send(404, "admin bridge not found", "text/plain; charset=utf-8")
            elif parsed.path.startswith("/assets/"):
                asset = safe_child(DOCS_DIR, parsed.path)
                if asset and asset.is_file():
                    self._send(200, asset.read_bytes(), content_type_for(asset))
                else:
                    self._send(404, "asset not found", "text/plain; charset=utf-8")
            elif parsed.path == "/report":
                report = latest_report()
                if report and Path(report["path"]).exists():
                    self._send(200, Path(report["path"]).read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(404, "No report", "text/plain; charset=utf-8")
            elif parsed.path == "/api/state":
                config, _ = load_yaml_file("config")
                texts = {key: read_text(spec["path"]) for key, spec in TEXT_FILES.items()}
                files = []
                for key, spec in TEXT_FILES.items():
                    path = spec["path"]
                    files.append({"key": key, "label": spec["label"], "exists": path.exists(), "mtime": path.stat().st_mtime if path.exists() else None})
                self._json({"success": True, "config": config, "texts": texts, "files": files, "report": latest_report(), "service": service_urls(self)})
            elif parsed.path == "/api/run":
                self._json({"success": True, **RUN_STATE})
            elif parsed.path == "/api/backups":
                self._json({"success": True, "items": list_backups()})
            else:
                self._json({"success": False, "error": "not found"}, 404)
        except Exception as exc:
            self._json({"success": False, "error": str(exc)}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api") and not self._authorized():
            self._json({"success": False, "error": "unauthorized"}, 401)
            return
        try:
            data = self._body()
            if parsed.path.startswith("/api/file/"):
                key = parsed.path.rsplit("/", 1)[-1]
                if key not in TEXT_FILES:
                    raise ValueError("unknown file")
                content = data.get("content", "")
                validate_file(key, content)
                path = TEXT_FILES[key]["path"]
                backup = backup_file(path, "save")
                atomic_write(path, content)
                self._json({"success": True, "backup": backup})
            elif parsed.path == "/api/validate":
                key = data.get("file", "config")
                validate_file(key, data.get("content", ""))
                self._json({"success": True})
            elif parsed.path == "/api/config/patch":
                config, _ = load_yaml_file("config")
                for key, value in data.get("patch", {}).items():
                    set_nested(config, key, value)
                content = dump_yaml(config)
                validate_file("config", content)
                backup = backup_file(TEXT_FILES["config"]["path"], "patch")
                atomic_write(TEXT_FILES["config"]["path"], content)
                self._json({"success": True, "backup": backup})
            elif parsed.path == "/api/run":
                started = run_crawl_background()
                self._json({"success": True, "started": started, **RUN_STATE})
            elif parsed.path == "/api/backup/restore":
                name = Path(data.get("name", "")).name
                src = BACKUP_DIR / name
                if not src.exists():
                    raise ValueError("backup not found")
                allowed_names = {spec["path"].name for spec in TEXT_FILES.values()}
                target_name = None
                for allowed in allowed_names:
                    if name.endswith("-" + allowed) or name == allowed:
                        target_name = allowed
                        break
                if not target_name:
                    raise ValueError("backup target not allowed")
                target = CONFIG_DIR / target_name
                backup_file(target, "pre-restore")
                shutil.copy2(src, target)
                self._json({"success": True})
            else:
                self._json({"success": False, "error": "not found"}, 404)
        except Exception as exc:
            self._json({"success": False, "error": str(exc)}, 400)


if __name__ == "__main__":
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if not ADMIN_TOKEN:
        raise SystemExit("TRENDRADAR_ADMIN_TOKEN or TRENDRADAR_ADMIN_TOKEN_FILE is required")
    host = os.environ.get("TRENDRADAR_ADMIN_HOST", "0.0.0.0")
    port = int(os.environ.get("TRENDRADAR_ADMIN_PORT", "8081"))
    print(f"TrendRadar Admin listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
