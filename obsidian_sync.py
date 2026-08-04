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
GITHUB_TODO_FILE = 'todos-data.json'
GITHUB_TODO_DIR = 'weekly-todo'  # GitHub 仓库中 TODO.md 的目录

# 本机 Obsidian vault 中的工作台笔记目录（NAS WebDAV 会同步到 LAPTOP）
OBSIDIAN_WORKBENCH_DIR = r'D:\obsidianData\个人知识库\workbench'
# 本机 Obsidian vault 中的周日程目录（TODO.md 存放位置）
OBSIDIAN_TODO_DIR = r'D:\obsidianData\个人知识库\2-日程\周日程'

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
# TODO SYNC
# ============================================================

def fetch_todo_files_from_github():
    """从 GitHub 获取 weekly-todo 目录下的所有 TODO.md 文件列表"""
    api_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_TODO_DIR}'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    try:
        req = urllib.request.Request(api_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        files = json.loads(resp.read())
        # 只返回 .md 文件
        return [f for f in files if f['name'].endswith('.md') and f['type'] == 'file']
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def fetch_file_content(download_url):
    """从 GitHub 下载文件内容"""
    try:
        req = urllib.request.Request(download_url)
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.read().decode('utf-8')
    except Exception:
        return None


def cmd_pull_todos():
    """从 GitHub 拉取 TODO.md → 写入 Obsidian 周日程目录"""
    print('📥 从 GitHub 拉取待办清单...')

    if not GITHUB_TOKEN:
        print('  ⚠️ 未设置 GITHUB_TOKEN 环境变量，无法拉取')
        return 0

    files = fetch_todo_files_from_github()
    if not files:
        print('  ℹ️ GitHub 上没有 TODO.md 文件')
        return 0

    todo_dir = OBSIDIAN_TODO_DIR
    os.makedirs(todo_dir, exist_ok=True)

    written = 0
    for f in files:
        filename = f['name']
        download_url = f['download_url']
        content = fetch_file_content(download_url)
        if not content:
            print(f'  ⚠️ 下载失败: {filename}')
            continue

        filepath = os.path.join(todo_dir, filename)

        # 检查是否有变化（避免无意义写入）
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as fp:
                if fp.read() == content:
                    continue

        with open(filepath, 'w', encoding='utf-8') as fp:
            fp.write(content)
        written += 1
        print(f'  ✅ {filename}')

    print(f'\n📊 拉取完成: 写入 {written} 个 TODO 文件')
    print(f'📁 → {todo_dir}')
    print('🔄 NAS WebDAV 将自动同步到 LAPTOP')
    return written


def cmd_push_todos():
    """读取 Obsidian 周日程目录的 TODO.md → 推送到 GitHub"""
    print('📤 推送待办清单到 GitHub...')

    if not GITHUB_TOKEN:
        print('  ⚠️ 未设置 GITHUB_TOKEN 环境变量，无法推送')
        return 0

    todo_dir = OBSIDIAN_TODO_DIR
    if not os.path.exists(todo_dir):
        print(f'  ⚠️ 目录不存在: {todo_dir}')
        return 0

    md_files = [f for f in os.listdir(todo_dir) if f.endswith('-TODO.md') or f.endswith('-DONE.md')]
    if not md_files:
        print('  ℹ️ 没有 TODO/DONE 文件')
        return 0

    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json'
    }

    pushed = 0
    for filename in md_files:
        filepath = os.path.join(todo_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        gh_path = f'{GITHUB_TODO_DIR}/{filename}'
        api_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{gh_path}'

        # 获取现有 SHA
        existing_sha = None
        try:
            req = urllib.request.Request(api_url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=15)
            existing_sha = json.loads(resp.read())['sha']
        except urllib.error.HTTPError:
            pass

        content_b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        body = json.dumps({
            'message': f'📋 同步 TODO.md: {filename} - {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            'content': content_b64,
            'branch': 'main',
            **({'sha': existing_sha} if existing_sha else {})
        }).encode('utf-8')

        try:
            req = urllib.request.Request(api_url, data=body, method='PUT', headers=headers)
            resp = urllib.request.urlopen(req, timeout=30)
            pushed += 1
            print(f'  ✅ {filename}')
        except Exception as e:
            print(f'  ❌ {filename}: {e}')

    print(f'\n📊 推送完成: {pushed}/{len(md_files)} 个文件')
    return pushed


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
    """双向同步：先 pull 再 push（笔记 + 待办）"""
    print('🔄 双向同步\n')
    print('═══ 笔记同步 ═══')
    pulled = cmd_pull()
    print()
    pushed = cmd_push()
    print()
    print('═══ 待办同步 ═══')
    todos_pulled = cmd_pull_todos()
    print()
    todos_pushed = cmd_push_todos()
    print(f'\n✅ 同步完成:')
    print(f'   笔记: 拉取 {pulled} 条, 推送 {pushed} 条')
    print(f'   待办: 拉取 {todos_pulled} 个, 推送 {todos_pushed} 个')
    print('🔄 NAS WebDAV 会自动将变更同步到其他设备')


def cmd_status():
    """查看同步状态"""
    state = load_sync_state()
    vault_path = OBSIDIAN_WORKBENCH_DIR
    todo_dir = OBSIDIAN_TODO_DIR

    print('📊 同步状态')
    print('=' * 50)
    print(f'\n📁 笔记目录: {vault_path}')
    print(f'   存在: {"✅" if os.path.exists(vault_path) else "❌"}')

    if os.path.exists(vault_path):
        md_files = [f for f in os.listdir(vault_path) if f.endswith('.md')]
        print(f'   笔记数: {len(md_files)}')
        for f in md_files[:5]:
            print(f'     · {f}')
        if len(md_files) > 5:
            print(f'     ... 还有 {len(md_files) - 5} 个')

    print(f'\n📁 待办目录: {todo_dir}')
    print(f'   存在: {"✅" if os.path.exists(todo_dir) else "❌"}')
    if os.path.exists(todo_dir):
        todo_files = [f for f in os.listdir(todo_dir) if f.endswith('-TODO.md') or f.endswith('-DONE.md')]
        print(f'   TODO 文件数: {len(todo_files)}')
        for f in sorted(todo_files):
            print(f'     · {f}')

    print(f'\n💾 本地缓存: {"有" if load_local_cache() else "无"}')
    print(f'🕐 上次 pull: {state.get("last_pull", "从未")}')
    print(f'🕐 上次 push: {state.get("last_push", "从未")}')

    print(f'\n🔗 GitHub 笔记: {GITHUB_REPO}/{GITHUB_NOTES_FILE}')
    try:
        data = fetch_from_github()
        if data:
            print(f'   云端笔记数: {data.get("count", len(data.get("notes", [])))}')
            print(f'   导出时间: {data.get("exported", "未知")}')
        else:
            print('   状态: 无文件')
    except Exception as e:
        print(f'   获取失败: {e}')

    print(f'\n🔗 GitHub 待办: {GITHUB_REPO}/{GITHUB_TODO_DIR}/')
    if GITHUB_TOKEN:
        try:
            todo_gh_files = fetch_todo_files_from_github()
            if todo_gh_files:
                print(f'   云端 TODO 文件数: {len(todo_gh_files)}')
                for f in todo_gh_files:
                    print(f'     · {f["name"]}')
            else:
                print('   状态: 无文件')
        except Exception as e:
            print(f'   获取失败: {e}')
    else:
        print('   ⚠️ 未设置 GITHUB_TOKEN，无法查看')

    print(f'\n🔄 NAS WebDAV 同步目标:')
    print(f'   LAPTOP: F:\\obsidian仓库\\个人知识库\\2-日程\\周日程')


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
        'pull-todos': cmd_pull_todos,
        'push-todos': cmd_push_todos,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f'未知命令: {cmd}')
        print('用法: python obsidian_sync.py [pull|push|sync|status|daemon|pull-todos|push-todos]')
        sys.exit(1)
