#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Obsidian ↔ 个人工作台 笔记同步脚本（单点版）

架构：
    浏览器 ──(导出JSON)──→ GitHub ──(pull)──→ 本脚本 ──→ Obsidian vault (yumg)
    Obsidian vault (yumg) ──(push)──→ 本脚本 ──→ GitHub ──(页面加载)──→ 浏览器
    
    NAS WebDAV 自动将 yumg 的 Obsidian vault 同步到 LAPTOP

用法：
    python obsidian_sync.py pull     # 从 GitHub 拉取 → 写入 Obsidian vault
    python obsidian_sync.py push     # 读取 Obsidian vault → 推送到 GitHub
    python obsidian_sync.py sync     # 双向同步
    python obsidian_sync.py status   # 查看同步状态
    python obsidian_sync.py daemon   # 守护模式，每 N 分钟自动同步

配置：
    修改下方 CONFIG 区域的参数
    GITHUB_TOKEN 通过环境变量 GITHUB_TOKEN 传入
"""

import os
import sys
import json
import re
import time
import base64
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO = 'Yumguo/personal-workbench'
GITHUB_NOTES_FILE = 'notes-data.json'

# 本机 Obsidian vault 中的工作台笔记目录（NAS WebDAV 会同步到 LAPTOP）
OBSIDIAN_WORKBENCH_DIR = r'D:\obsidianData\个人知识库\workbench'

# 本地缓存（用于增量同步 + 离线容灾）
LOCAL_CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_CACHE_FILE = os.path.join(LOCAL_CACHE_DIR, 'notes-data.json')
SYNC_STATE_FILE = os.path.join(LOCAL_CACHE_DIR, '.sync_state.json')

# 守护模式间隔（分钟）
DAEMON_INTERVAL = 15

# 分类映射
CAT_TO_TAG = {'work': '工作', 'study': '学习', 'idea': '灵感', 'other': '其他'}
TAG_TO_CAT = {v: k for k, v in CAT_TO_TAG.items()}


# ============================================================
# HELPERS
# ============================================================

def ensure_vault_dir():
    """确保 vault 目录存在"""
    os.makedirs(OBSIDIAN_WORKBENCH_DIR, exist_ok=True)
    return OBSIDIAN_WORKBENCH_DIR


def safe_filename(name):
    """标题 → 安全文件名"""
    name = re.sub(r'[\\/:*?"<>|]', '_', name or 'untitled')
    return name[:60].strip('_. ')


def load_sync_state():
    """加载同步状态（记录上次同步时间、已知笔记列表）"""
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {'last_pull': None, 'last_push': None, 'known_note_ids': []}


def save_sync_state(state):
    """保存同步状态"""
    with open(SYNC_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def note_to_markdown(note):
    """笔记对象 → Obsidian Markdown（含 YAML frontmatter）"""
    cat_tag = CAT_TO_TAG.get(note.get('category', 'other'), '其他')
    created = note.get('createdAt', '').replace('T', ' ').split('.')[0]
    updated = note.get('updatedAt', '').replace('T', ' ').split('.')[0] or created
    title = note.get('title', '无标题')
    content = note.get('content', '')

    lines = [
        '---',
        f'title: "{title}"',
        f'category: {cat_tag}',
        f'tags: [{cat_tag}, workbench]',
        f'created: {created}',
        f'updated: {updated}',
        'source: personal-workbench',
        f'id: {note.get("id", "")}',
        '---',
        '',
        f'# {title}',
        '',
        content,
        ''
    ]
    return '\n'.join(lines)


def parse_markdown_to_note(filepath):
    """Obsidian .md → 笔记对象"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    title = ''
    category = 'other'
    body = content
    created = datetime.fromtimestamp(os.path.getctime(filepath)).isoformat()
    updated = datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat()
    note_id = ''

    fm_match = re.match(r'^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$', content)
    if fm_match:
        fm = fm_match.group(1)
        body = fm_match.group(2)

        title_m = re.search(r'title:\s*"?([^"\n]+)"?', fm)
        if title_m:
            title = title_m.group(1).strip()

        cat_m = re.search(r'category:\s*(.+)', fm)
        if cat_m:
            category = TAG_TO_CAT.get(cat_m.group(1).strip(), 'other')

        created_m = re.search(r'created:\s*(.+)', fm)
        if created_m:
            try:
                created = datetime.fromisoformat(created_m.group(1).strip().replace(' ', 'T')).isoformat()
            except:
                pass

        updated_m = re.search(r'updated:\s*(.+)', fm)
        if updated_m:
            try:
                updated = datetime.fromisoformat(updated_m.group(1).strip().replace(' ', 'T')).isoformat()
            except:
                pass

        id_m = re.search(r'id:\s*(.+)', fm)
        if id_m:
            note_id = id_m.group(1).strip()

        body = re.sub(r'^#\s+.+\n?', '', body).strip()
    else:
        title = os.path.splitext(os.path.basename(filepath))[0].replace('_', ' ')
        first_line = re.search(r'^#\s+(.+)', content, re.MULTILINE)
        if first_line:
            title = first_line.group(1).strip()
            body = re.sub(r'^#\s+.+\n?', '', content).strip()

    if not note_id:
        note_id = 'n' + str(int(datetime.now().timestamp() * 1000)) + '_' + os.path.basename(filepath)[:8]

    return {
        'id': note_id,
        'title': title or '无标题',
        'category': category,
        'content': body,
        'createdAt': created,
        'updatedAt': updated
    }


# ============================================================
# GITHUB
# ============================================================

def fetch_from_github():
    """从 GitHub 获取 notes-data.json"""
    url = f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_NOTES_FILE}'
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def push_to_github(notes_data):
    """推送 notes-data.json 到 GitHub（Contents API）"""
    content = json.dumps(notes_data, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')

    api_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_NOTES_FILE}'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json'
    }

    # 获取现有文件 SHA
    existing_sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        existing_sha = json.loads(resp.read())['sha']
    except urllib.error.HTTPError:
        pass

    body = json.dumps({
        'message': f'📝 同步工作台笔记 ({notes_data.get("count", 0)} 条) - {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        'content': content_b64,
        'branch': 'main',
        **({'sha': existing_sha} if existing_sha else {})
    }).encode('utf-8')

    req = urllib.request.Request(api_url, data=body, method='PUT', headers=headers)
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())['content']['sha']


def load_local_cache():
    if os.path.exists(LOCAL_CACHE_FILE):
        with open(LOCAL_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def save_local_cache(notes_data):
    with open(LOCAL_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(notes_data, ensure_ascii=False, indent=2, fp=f)


# ============================================================
# COMMANDS
# ============================================================

def cmd_pull():
    """GitHub → Obsidian vault"""
    print('📥 从 GitHub 拉取笔记...')

    data = None
    try:
        data = fetch_from_github()
    except Exception as e:
        print(f'  ⚠️ GitHub 获取失败: {e}')

    if not data:
        data = load_local_cache()

    if not data or not data.get('notes'):
        print('  ℹ️ 没有可拉取的笔记')
        return 0

    notes = data['notes']
    vault_path = ensure_vault_dir()
    written = 0
    skipped = 0

    # 记录 vault 中现有的文件（用于检测被删除的笔记）
    existing_files = {f for f in os.listdir(vault_path) if f.endswith('.md')}
    expected_files = set()

    for note in notes:
        filename = safe_filename(note.get('title', 'untitled')) + '.md'
        filepath = os.path.join(vault_path, filename)
        expected_files.add(filename)
        md_content = note_to_markdown(note)

        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                if f.read() == md_content:
                    skipped += 1
                    continue

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(md_content)
        written += 1
        print(f'  ✅ {note.get("title", "untitled")}')

    # 处理云端已删除但本地仍存在的笔记（只处理 source=workbench 的）
    orphaned = existing_files - expected_files
    deleted = 0
    for fname in orphaned:
        fpath = os.path.join(vault_path, fname)
        with open(fpath, 'r', encoding='utf-8') as f:
            content = f.read()
        if 'source: personal-workbench' in content:
            os.remove(fpath)
            deleted += 1
            print(f'  🗑️ 已删除（云端已不存在）: {fname}')

    save_local_cache(data)
    state = load_sync_state()
    state['last_pull'] = datetime.now().isoformat()
    state['known_note_ids'] = [n['id'] for n in notes]
    save_sync_state(state)

    print(f'\n📊 拉取完成: 写入 {written}, 跳过 {skipped}, 删除 {deleted}')
    print(f'📁 → {vault_path}')
    print('🔄 NAS WebDAV 将自动同步到 LAPTOP')
    return written


def cmd_push():
    """Obsidian vault → GitHub"""
    print('📤 推送笔记到 GitHub...')

    vault_path = ensure_vault_dir()
    md_files = [f for f in os.listdir(vault_path) if f.endswith('.md')]
    if not md_files:
        print('  ℹ️ vault 目录无 .md 文件')
        return 0

    notes = []
    for f in md_files:
        try:
            notes.append(parse_markdown_to_note(os.path.join(vault_path, f)))
        except Exception as e:
            print(f'  ⚠️ 解析失败 {f}: {e}')

    # 合并缓存中已有的笔记（避免丢失只有浏览器端才有的旧笔记）
    cache = load_local_cache()
    if cache and cache.get('notes'):
        existing_ids = {n['id'] for n in notes}
        for old in cache['notes']:
            if old['id'] not in existing_ids:
                notes.append(old)

    notes_data = {
        'exported': datetime.now().isoformat(),
        'count': len(notes),
        'notes': notes
    }

    if not GITHUB_TOKEN:
        print('  ⚠️ 未设置 GITHUB_TOKEN 环境变量，仅保存到本地缓存')
        save_local_cache(notes_data)
        return 0

    try:
        sha = push_to_github(notes_data)
        save_local_cache(notes_data)
        state = load_sync_state()
        state['last_push'] = datetime.now().isoformat()
        state['known_note_ids'] = [n['id'] for n in notes]
        save_sync_state(state)
        print(f'\n📊 推送完成: {len(notes)} 条笔记')
        print(f'🔗 SHA: {sha[:12]}')
        return len(notes)
    except Exception as e:
        print(f'  ❌ 推送失败: {e}')
        save_local_cache(notes_data)
        return 0


def cmd_sync():
    """双向同步：先 pull 再 push"""
    print('🔄 双向同步\n')
    pulled = cmd_pull()
    print()
    pushed = cmd_push()
    print(f'\n✅ 同步完成: 拉取 {pulled} 条, 推送 {pushed} 条')
    print('🔄 NAS WebDAV 会自动将变更同步到其他设备')


def cmd_status():
    """查看同步状态"""
    state = load_sync_state()
    vault_path = OBSIDIAN_WORKBENCH_DIR

    print('📊 同步状态')
    print('=' * 50)
    print(f'📁 Obsidian vault: {vault_path}')
    print(f'   存在: {"✅" if os.path.exists(vault_path) else "❌"}')

    if os.path.exists(vault_path):
        md_files = [f for f in os.listdir(vault_path) if f.endswith('.md')]
        print(f'   笔记数: {len(md_files)}')
        for f in md_files[:5]:
            print(f'     · {f}')
        if len(md_files) > 5:
            print(f'     ... 还有 {len(md_files) - 5} 个')

    print(f'\n💾 本地缓存: {"有" if load_local_cache() else "无"}')
    print(f'🕐 上次 pull: {state.get("last_pull", "从未")}')
    print(f'🕐 上次 push: {state.get("last_push", "从未")}')

    print(f'\n🔗 GitHub: {GITHUB_REPO}/{GITHUB_NOTES_FILE}')
    try:
        data = fetch_from_github()
        if data:
            print(f'   云端笔记数: {data.get("count", len(data.get("notes", [])))}')
            print(f'   导出时间: {data.get("exported", "未知")}')
        else:
            print('   状态: 无文件')
    except Exception as e:
        print(f'   获取失败: {e}')

    print(f'\n🔄 NAS WebDAV 同步目标:')
    print(f'   LAPTOP: F:\\obsidian仓库\\个人知识库\\4-知识积累\\02 source note\\workbench')


def cmd_daemon():
    """守护模式：定时自动同步"""
    print(f'🔄 守护模式启动，每 {DAEMON_INTERVAL} 分钟同步一次')
    print('   按 Ctrl+C 退出\n')

    while True:
        try:
            now = datetime.now().strftime('%H:%M:%S')
            print(f'[{now}] ─── 开始同步 ───')
            cmd_sync()
            print()
        except KeyboardInterrupt:
            print('\n👋 守护模式已退出')
            break
        except Exception as e:
            print(f'  ❌ 同步出错: {e}\n')

        time.sleep(DAEMON_INTERVAL * 60)


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'

    commands = {
        'pull': cmd_pull,
        'push': cmd_push,
        'sync': cmd_sync,
        'status': cmd_status,
        'daemon': cmd_daemon,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f'未知命令: {cmd}')
        print('用法: python obsidian_sync.py [pull|push|sync|status|daemon]')
        sys.exit(1)
