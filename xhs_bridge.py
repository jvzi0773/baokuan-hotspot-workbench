# -*- coding: utf-8 -*-
"""
xhs_bridge.py — 小红书采集本地桥接服务（基于 Spider_XHS）
========================================================
让「自媒体工作台」页面通过本地 HTTP 调用小红书采集能力：
  纯 HTTP 签名（无需浏览器），支持 搜索笔记 / 笔记详情(无水印图&视频) /
  用户主页笔记 / 评论采集 / 导出 CSV。

接口：
  GET  /health                    服务状态 + 登录状态
  POST /login/qr/start            生成二维码（后台线程等待扫码）
  GET  /login/qr.png              当前二维码图片
  POST /login/qr/status           二维码扫码状态轮询
  POST /login/cookie   {cookies}  用完整 Cookie 登录
  POST /logout                    退出登录
  POST /search   {q,num,sort}     关键词搜索笔记
  POST /note     {url}            笔记详情（无水印图片/视频）
  POST /user     {url}            用户主页笔记
  POST /comments {url}            笔记全部评论

登录态自动保存到本脚本同目录 xhs_cookies.json，重启后自动恢复。

用法：
    python xhs_bridge.py                 # 默认 127.0.0.1:8811
    python xhs_bridge.py --port 9000
"""
import argparse
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ---------------- 定位 Spider_XHS ----------------
HERE = os.path.dirname(os.path.abspath(__file__))
SPIDER_DIR = None
for _c in (HERE, os.path.join(HERE, 'Spider_XHS'), os.path.join(os.path.dirname(HERE), 'Spider_XHS')):
    if os.path.isdir(os.path.join(_c, 'xhs_utils')):
        SPIDER_DIR = _c
        break
if SPIDER_DIR:
    sys.path.insert(0, SPIDER_DIR)

COOKIE_FILE = os.path.join(HERE, 'xhs_cookies.json')

# ---------------- 全局状态 ----------------
_auth = None        # XHSPcAuth
_api = None         # XHS_Apis
_login_info = {'nickname': '', 'red_id': ''}
_api_lock = threading.Lock()
_qr = {'status': 'idle', 'msg': '', 'qr_id': '', 'code': '', 'qr_url': '', 'nickname': ''}
_qr_lock = threading.Lock()


def _spider_ready():
    try:
        from apis.xhs_pc_apis import XHS_Apis  # noqa
        from xhs_utils.xhs_pc import XHSPcAuth  # noqa
        return True
    except Exception:
        return False


def _save_cookies(cookies_str):
    try:
        with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'cookies': cookies_str, 'time': int(time.time())}, f, ensure_ascii=False)
    except Exception:
        pass


def _load_cookies():
    try:
        with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('cookies', '')
    except Exception:
        return ''


def _set_api_from_cookies(cookies_str):
    """用 Cookie 构建 XHSPcAuth + XHS_Apis 并 bootstrap。"""
    global _auth, _api, _login_info
    from xhs_utils.xhs_pc import XHSPcAuth
    from apis.xhs_pc_apis import XHS_Apis
    auth = XHSPcAuth.from_cookie(cookies_str)
    api = XHS_Apis(auth).bootstrap()
    try:
        ok, msg, res = api.get_user_me()
        data = (res or {}).get('data') or {}
        if ok and data:
            _login_info['nickname'] = data.get('nickname') or _login_info.get('nickname', '')
            _login_info['red_id'] = data.get('user_id') or ''
    except Exception:
        pass
    _auth, _api = auth, api
    _save_cookies(cookies_str)
    return True


def _restore_session():
    """启动时自动恢复已保存的登录态。"""
    cookies = _load_cookies()
    if not cookies:
        return
    try:
        _set_api_from_cookies(cookies)
    except Exception as e:
        print(f'[xhs] 恢复登录态失败: {e}')


# ---------------- 二维码登录（后台线程） ----------------
def _qr_worker():
    from apis.xhs_pc_login_apis import XHSLoginApi
    try:
        with _qr_lock:
            _qr['status'] = 'init'
            _qr['msg'] = '正在初始化设备...'
        login = XHSLoginApi(proxies=None)
        cookies = login.generate_init_cookies()
        ok, msg, qr_data = login.generate_qrcode(cookies)
        if not ok:
            with _qr_lock:
                _qr['status'] = 'error'
                _qr['msg'] = f'获取二维码失败: {msg}'
            return
        cookies = qr_data['cookies']
        with _qr_lock:
            _qr.update(status='waiting', msg='请用小红书App扫码',
                       qr_id=qr_data['qr_id'], code=qr_data['code'], qr_url=qr_data['qr_url'])
        # 预检查（与官方流程一致：先匿名轮询一次 + webprofile）
        ok, msg, cookies = login.check_qrcode_status(qr_data['qr_id'], qr_data['code'], cookies)
        if msg != '请扫描二维码':
            with _qr_lock:
                _qr['status'] = 'error'
                _qr['msg'] = f'二维码状态异常: {msg}'
            return
        login.ensure_webprofile(cookies)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            time.sleep(2)
            ok, msg, cookies = login.check_qrcode_status(qr_data['qr_id'], qr_data['code'], cookies)
            if ok:
                with _qr_lock:
                    _qr['msg'] = '扫码成功，正在验证登录...'
                break
            if msg == '二维码已过期':
                with _qr_lock:
                    _qr['status'] = 'expired'
                    _qr['msg'] = '二维码已过期，请重新生成'
                return
        else:
            with _qr_lock:
                _qr['status'] = 'timeout'
                _qr['msg'] = '等待扫码超时，请重新生成'
            return
        ok, user_info, cookies = login.get_user_info(cookies)
        if not ok or user_info.get('guest') is not False:
            with _qr_lock:
                _qr['status'] = 'error'
                _qr['msg'] = '会话验证失败，请重新登录'
            return
        cookies_str = login.cookies_to_str(cookies)
        _set_api_from_cookies(cookies_str)
        with _qr_lock:
            _qr['status'] = 'done'
            _qr['msg'] = '登录成功'
            _qr['nickname'] = user_info.get('nickname', '') or _login_info.get('nickname', '')
    except Exception as e:
        with _qr_lock:
            _qr['status'] = 'error'
            _qr['msg'] = str(e)


def _qr_start():
    with _qr_lock:
        if _qr['status'] in ('waiting', 'init'):
            return {'ok': False, 'msg': '二维码已存在，请直接扫码'}
    threading.Thread(target=_qr_worker, daemon=True).start()
    return {'ok': True, 'msg': '正在生成二维码...'}


# ---------------- 数据归一化 ----------------
def _n(obj, *keys, default=''):
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur and cur[k] is not None:
            cur = cur[k]
        else:
            return default
    return cur


def _num(v):
    try:
        return int(v)
    except Exception:
        return 0


def _norm_note(it):
    card = it.get('note_card') or it
    note_id = str(card.get('note_id') or it.get('id') or '')
    cover = ''
    cov = card.get('cover') or {}
    info = cov.get('info_list') or []
    if info and isinstance(info[0], dict) and info[0].get('url'):
        cover = info[0]['url']
    elif cov.get('url_default'):
        cover = cov['url_default']
    inter = card.get('interact_info') or {}
    return {
        'note_id': note_id,
        'title': card.get('display_title') or card.get('title') or '',
        'desc': card.get('desc') or '',
        'url': 'https://www.xiaohongshu.com/explore/' + note_id,
        'cover': cover,
        'likes': _num(inter.get('liked_count')),
        'collects': _num(inter.get('collected_count')),
        'comments': _num(inter.get('comment_count')),
        'author': _n(card, 'user', 'nickname'),
        'author_id': _n(card, 'user', 'user_id'),
        'type': card.get('type', 'normal'),
        'time': card.get('time', ''),
    }


def _norm_detail(res_json):
    note = _n(res_json, 'data', 'note', default=None)
    if not isinstance(note, dict):
        return None
    from apis.xhs_pc_apis import XHS_Apis
    nid = str(note.get('note_id') or note.get('id') or '')
    images = []
    for img in note.get('image_list') or []:
        if not isinstance(img, dict):
            continue
        url = img.get('url_default') or _n(img, 'info_list', 0, 'url')
        if url:
            clean = XHS_Apis.get_note_no_water_img(url)[2] or url
            images.append(clean)
    video = ''
    if note.get('type') == 'video' and nid:
        video = XHS_Apis.get_note_no_water_video(nid)[2] or ''
    inter = note.get('interact_info') or {}
    tags = [t.get('name', '') for t in (note.get('tag_list') or []) if isinstance(t, dict)]
    return {
        'note_id': nid,
        'title': note.get('title', ''),
        'desc': note.get('desc', ''),
        'url': 'https://www.xiaohongshu.com/explore/' + nid,
        'type': note.get('type', 'normal'),
        'images': images,
        'video': video,
        'likes': _num(inter.get('liked_count')),
        'collects': _num(inter.get('collected_count')),
        'comments': _num(inter.get('comment_count')),
        'author': _n(note, 'user', 'nickname'),
        'author_id': _n(note, 'user', 'user_id'),
        'tags': tags,
        'time': note.get('time', ''),
    }


def _norm_comments(cmt_list):
    out = []
    for c in cmt_list or []:
        if not isinstance(c, dict):
            continue
        subs = []
        for s in c.get('sub_comments') or []:
            if isinstance(s, dict):
                subs.append({
                    'content': s.get('content', ''),
                    'nickname': _n(s, 'user_info', 'nickname'),
                    'likes': _num(s.get('like_count')),
                    'time': s.get('time', 0),
                })
        out.append({
            'id': c.get('id', ''),
            'content': c.get('content', ''),
            'nickname': _n(c, 'user_info', 'nickname'),
            'avatar': _n(c, 'user_info', 'avatar'),
            'likes': _num(c.get('like_count')),
            'time': c.get('time', 0),
            'subs': subs,
        })
    return out


# ---------------- 采集动作 ----------------
def do_search(q, num, sort):
    api = _api
    if api is None:
        return {'ok': False, 'msg': '未登录'}
    sort_map = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}
    with _api_lock:
        ok, msg, notes = api.search_some_note(q, int(num), sort_map.get(int(sort), 0))
    if not ok:
        return {'ok': False, 'msg': msg}
    return {'ok': True, 'notes': [_norm_note(x) for x in notes]}


def do_note(url):
    api = _api
    if api is None:
        return {'ok': False, 'msg': '未登录'}
    with _api_lock:
        ok, msg, res = api.get_note_info(url)
    if not ok:
        return {'ok': False, 'msg': msg}
    detail = _norm_detail(res)
    if detail is None:
        return {'ok': False, 'msg': '笔记解析失败'}
    return {'ok': True, 'note': detail}


def do_user(url):
    api = _api
    if api is None:
        return {'ok': False, 'msg': '未登录'}
    with _api_lock:
        ok, msg, notes = api.get_user_all_notes(url)
    if not ok:
        return {'ok': False, 'msg': msg}
    return {'ok': True, 'notes': [_norm_note(x) for x in notes]}


def do_comments(url):
    api = _api
    if api is None:
        return {'ok': False, 'msg': '未登录'}
    with _api_lock:
        ok, msg, comments = api.get_note_all_comment(url)
    if not ok:
        return {'ok': False, 'msg': msg}
    return {'ok': True, 'comments': _norm_comments(comments)}


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(200, b'')

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path)
        if path.path == '/health':
            self._send(200, {
                'ok': True,
                'spider': _spider_ready(),
                'login': _api is not None,
                'nickname': _login_info.get('nickname', ''),
                'qr_status': _qr['status'],
                'port': self.server.server_port,
            })
            return
        if path.path == '/login/qr.png':
            with _qr_lock:
                qr_url = _qr.get('qr_url', '')
            if not qr_url:
                self._send(404, {'ok': False, 'msg': '二维码不存在，请先 POST /login/qr/start'})
                return
            try:
                import qrcode
                qr = qrcode.QRCode(box_size=10, border=4)
                qr.add_data(qr_url)
                qr.make(fit=True)
                img = qr.make_image(fill_color='black', back_color='white')
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                self._send(200, buf.getvalue(), 'image/png')
            except Exception as e:
                self._send(500, {'ok': False, 'msg': str(e)})
            return
        self._send(404, {'ok': False, 'msg': 'not found'})

    def do_POST(self):
        global _auth, _api
        path = urlparse(self.path).path
        body = self._body()
        if path == '/login/qr/start':
            self._send(200, _qr_start())
            return
        if path == '/login/qr/status':
            with _qr_lock:
                self._send(200, {
                    'status': _qr['status'],
                    'msg': _qr['msg'],
                    'nickname': _qr['nickname'],
                    'login': _api is not None,
                })
            return
        if path == '/login/cookie':
            cookies = str(body.get('cookies') or '').strip()
            if not cookies:
                self._send(400, {'ok': False, 'msg': '缺少 cookies'})
                return
            try:
                _set_api_from_cookies(cookies)
                self._send(200, {'ok': True, 'msg': '登录成功', 'nickname': _login_info.get('nickname', '')})
            except Exception as e:
                self._send(400, {'ok': False, 'msg': f'登录失败: {e}'})
            return
        if path == '/logout':
            _auth, _api = None, None
            try:
                if os.path.exists(COOKIE_FILE):
                    os.remove(COOKIE_FILE)
            except Exception:
                pass
            with _qr_lock:
                _qr.update(status='idle', msg='', qr_url='', nickname='')
            self._send(200, {'ok': True, 'msg': '已退出登录'})
            return
        if path == '/search':
            self._send(200, do_search(body.get('q', ''), body.get('num', 10), body.get('sort', 0)))
            return
        if path == '/note':
            self._send(200, do_note(body.get('url', '')))
            return
        if path == '/user':
            self._send(200, do_user(body.get('url', '')))
            return
        if path == '/comments':
            self._send(200, do_comments(body.get('url', '')))
            return
        self._send(404, {'ok': False, 'msg': 'not found'})


def main():
    parser = argparse.ArgumentParser(description='小红书采集本地桥接服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8811)
    args = parser.parse_args()

    if not SPIDER_DIR:
        print('[xhs] 未找到 Spider_XHS 库（需要 xhs_utils 目录），请把本脚本放到 Spider_XHS 目录运行。')
        return
    if not _spider_ready():
        print('[xhs] Spider_XHS 依赖未安装，请先执行: pip install -r requirements.txt')
        return

    _restore_session()
    state = '已登录' if _api is not None else '未登录'
    print(f'[xhs] 小红书采集桥接服务已启动 http://{args.host}:{args.port}  (登录状态: {state})')
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
