#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Obsidian ↔ 个人工作台 笔记同步脚本

功能：
1. 从 GitHub 拉取 notes-data.json → 转换为 .md 文件写入 Obsidian vault
2. 扫描 Obsidian vault 中 workbench/ 目录的 .md 文件 → 更新 notes-data.json 并推送到 GitHub

用法：
    python obsidian_sync.py pull    # 从云端拉取笔记到 Obsidian
    python obsidian_sync.py push    # 从 Obsidian 推送笔记到云端
    python obsidian_sync.py sync    # 双向同步
    python obsidian_sync.py status  # 查看同步状态

配置：
    修改下方 CONFIG 区域的参数
"""

import os
import sys
import json
import re
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

# Obsidian vault 路径（根据当前机器自动选择）
OBSIDIAN_VAULT_PATHS = [
    r'D:\obsidianData\个人知识库\4-知识积累\02 source note\workbench',
    r'F:\obsidian仓库\个人知识库\4-知识积累\02 source note\workbench',
]

# 本地缓存的 notes-data.json 路径
LOCAL_CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_CACHE_FILE = os.path.join(LOCAL_CACHE_DIR, 'notes-data.json')

# 分类映射
CAT_TO_TAG = {'work': '工作', 'study': '学习', 'idea': '灵感', 'other': '其他'}
TAG_TO_CAT = {v: k for k, v in CAT_TO_TAG.items()}

# ============================================================
# HELPERS
# ============================================================

def get_vault_path():
    """获取可用的 Obsidian vault 路径"""
    for p in OBSIDIAN_VAULT_PATHS:
        parent = os.path.dirname(p)
        if os.path.exists(parent):
            os.makedirs(p, exist_ok=True)
            return p
    # 如果都不存在，使用第一个并创建
    p = OBSIDIAN_VAULT_PATHS[0]
    os.makedirs(p, exist_ok=True)
    return p


def safe_filename(name):
    """将标题转为安全的文件名"""
    name = re.sub(r'[\\/:*?"<>|]', '_', name or 'untitled')
    return name[:60]


def note_to_markdown(note):
    """将笔记对象转为 Obsidian 兼容的 Markdown（含 YAML frontmatter）"""
    cat_tag = CAT_TO_TAG.get(note.get('category', 'other'), '其他')
    created = note.get('createdAt', '')
    if created:
        created = created.replace('T', ' ').split('.')[0]
    updated = note.get('updatedAt', '')
    if updated:
        updated = updated.replace('T', ' ').split('.')[0]
    else:
        updated = created

    lines = [
        '---',
        f'title: "{note.get("title", "").replace('"', '\\"')}"',
        f'category: {cat_tag}',
        f'tags: [{cat_tag}, workbench]',
        f'created: {created}',
        f'updated: {updated}',
        'source: personal-workbench',
        f'id: {note.get("id", "")}',
        '---',
        '',
        f'# {note.get("title", "无标题")}',
        '',
    ]
    if note.get('content'):
        lines.append(note['content'])
    lines.append('')
    return '\n'.join(lines)


def parse_markdown_to_note(filepath):
    """从 Obsidian .md 文件解析出笔记对象"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    title = ''
    category = 'other'
    body = content
    created = datetime.now().isoformat()
    updated = created
    note_id = ''

    # 解析 YAML frontmatter
    fm_match = re.match(r'^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$', content)
    if fm_match:
        fm = fm_match.group(1)
        body = fm_match.group(2)

        title_m = re.search(r'title:\s*"?([^"\n]+)"?', fm)
        if title_m:
            title = title_m.group(1).strip()

        cat_m = re.search(r'category:\s*(.+)', fm)
        if cat_m:
            cat_val = cat_m.group(1).strip()
            category = TAG_TO_CAT.get(cat_val, 'other')

        created_m = re.search(r'created:\s*(.+)', fm)
        if created_m:
            d = created_m.group(1).strip()
            try:
                created = datetime.fromisoformat(d.replace(' ', 'T')).isoformat()
            except:
                pass

        updated_m = re.search(r'updated:\s*(.+)', fm)
        if updated_m:
            d = updated_m.group(1).strip()
            try:
                updated = datetime.fromisoformat(d.replace(' ', 'T')).isoformat()
            except:
                pass

        id_m = re.search(r'id:\s*(.+)', fm)
        if id_m:
            note_id = id_m.group(1).strip()

        # 去掉 # 标题行
        body = re.sub(r'^#\s+.+\n?', '', body).strip()
    else:
        # 没有 frontmatter，用文件名做标题
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


def fetch_from_github():
    """从 GitHub 获取 notes-data.json"""
    url = f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_NOTES_FILE}'
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print('  ℹ️ 云端暂无 notes-data.json')
            return None
        raise


def push_to_github(notes_data):
    """将 notes-data.json 推送到 GitHub"""
    content = json.dumps(notes_data, ensure_ascii=False, indent=2)
    content_b64 = json.dumps(content.encode('utf-8').hex())  # Not used, we'll use base64

    import base64
    content_b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')

    # 获取当前文件 SHA
    api_url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_NOTES_FILE}'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }

    existing_sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        existing_sha = json.loads(resp.read())['sha']
    except urllib.error.HTTPError:
        pass  # 文件不存在，新建

    # 推送
    body = {
        'message': f'📝 同步工作台笔记 ({notes_data.get("count", len(notes_data.get("notes", [])))} 条) - {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        'content': content_b64,
        'branch': 'main'
    }
    if existing_sha:
        body['sha'] = existing_sha

    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(api_url, data=data, method='PUT', headers={
        'Authorization': f'token {GITHUB_TOKEN}',
        'Content-Type': 'application/json'
    })
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())
    return result['content']['sha']


def load_local_cache():
    """加载本地缓存的 notes-data.json"""
    if os.path.exists(LOCAL_CACHE_FILE):
        with open(LOCAL_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def save_local_cache(notes_data):
    """保存本地缓存"""
    with open(LOCAL_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(notes_data, ensure_ascii=False, indent=2, fp=f)


# ============================================================
# COMMANDS
# ============================================================

def cmd_pull():
    """从云端拉取笔记到 Obsidian vault"""
    print('📥 从云端拉取笔记...')

    # 尝试从 GitHub 获取，失败则用本地缓存
    data = None
    try:
        data = fetch_from_github()
    except Exception as e:
        print(f'  ⚠️ 从 GitHub 获取失败: {e}')

    if not data:
        data = load_local_cache()

    if not data or not data.get('notes'):
        print('  ℹ️ 没有可拉取的笔记')
        return

    notes = data['notes']
    vault_path = get_vault_path()
    written = 0
    skipped = 0

    for note in notes:
        filename = safe_filename(note.get('title', 'untitled')) + '.md'
        filepath = os.path.join(vault_path, filename)
        md_content = note_to_markdown(note)

        # 检查是否已存在且内容相同
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                existing = f.read()
            if existing == md_content:
                skipped += 1
                continue

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(md_content)
        written += 1
        print(f'  ✅ {note.get("title", "untitled")}')

    # 保存本地缓存
    save_local_cache(data)

    print(f'\n📊 拉取完成: 写入 {written} 个文件, 跳过 {skipped} 个未变更文件')
    print(f'📁 目标目录: {vault_path}')


def cmd_push():
    """从 Obsidian vault 推送笔记到云端"""
    print('📤 推送笔记到云端...')

    vault_path = get_vault_path()
    if not os.path.exists(vault_path):
        print(f'  ❌ vault 目录不存在: {vault_path}')
        return

    # 扫描 .md 文件
    md_files = [f for f in os.listdir(vault_path) if f.endswith('.md')]
    if not md_files:
        print('  ℹ️ vault 目录中没有 .md 文件')
        return

    notes = []
    for f in md_files:
        filepath = os.path.join(vault_path, f)
        try:
            note = parse_markdown_to_note(filepath)
            notes.append(note)
        except Exception as e:
            print(f'  ⚠️ 解析失败 {f}: {e}')

    # 加载已有数据合并
    existing = load_local_cache()
    if existing and existing.get('notes'):
        existing_ids = {n['id'] for n in notes}
        for old_note in existing['notes']:
            if old_note['id'] not in existing_ids:
                notes.append(old_note)

    notes_data = {
        'exported': datetime.now().isoformat(),
        'count': len(notes),
        'notes': notes
    }

    # 推送到 GitHub
    try:
        sha = push_to_github(notes_data)
        save_local_cache(notes_data)
        print(f'\n📊 推送完成: {len(notes)} 条笔记')
        print(f'🔗 GitHub SHA: {sha}')
    except Exception as e:
        print(f'  ❌ 推送失败: {e}')
        # 保存到本地缓存
        save_local_cache(notes_data)
        print(f'  💾 已保存到本地缓存: {LOCAL_CACHE_FILE}')


def cmd_sync():
    """双向同步"""
    print('🔄 双向同步...')
    print()
    cmd_pull()
    print()
    cmd_push()
    print()
    print('✅ 双向同步完成')


def cmd_status():
    """查看同步状态"""
    print('📊 同步状态')
    print('=' * 50)

    vault_path = get_vault_path()
    print(f'📁 Obsidian vault: {vault_path}')
    print(f'   存在: {"✅" if os.path.exists(vault_path) else "❌"}')

    if os.path.exists(vault_path):
        md_files = [f for f in os.listdir(vault_path) if f.endswith('.md')]
        print(f'   笔记数量: {len(md_files)}')
        for f in md_files[:5]:
            print(f'     - {f}')
        if len(md_files) > 5:
            print(f'     ... 还有 {len(md_files) - 5} 个文件')

    print()
    cache = load_local_cache()
    if cache:
        print(f'💾 本地缓存: {LOCAL_CACHE_FILE}')
        print(f'   笔记数量: {cache.get("count", len(cache.get("notes", [])))}')
        print(f'   导出时间: {cache.get("exported", "未知")}')
    else:
        print('💾 本地缓存: 无')

    print()
    print(f'🔗 GitHub: {GITHUB_REPO}/{GITHUB_NOTES_FILE}')
    try:
        data = fetch_from_github()
        if data:
            print(f'   笔记数量: {data.get("count", len(data.get("notes", [])))}')
            print(f'   导出时间: {data.get("exported", "未知")}')
        else:
            print('   状态: 无文件')
    except Exception as e:
        print(f'   获取失败: {e}')


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'

    if cmd == 'pull':
        cmd_pull()
    elif cmd == 'push':
        cmd_push()
    elif cmd == 'sync':
        cmd_sync()
    elif cmd == 'status':
        cmd_status()
    else:
        print(f'未知命令: {cmd}')
        print('用法: python obsidian_sync.py [pull|push|sync|status]')
        sys.exit(1)
