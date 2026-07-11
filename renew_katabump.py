#!/usr/bin/env python3
"""
KataBump 自动续期脚本 (基于 undetected-chromedriver)

参考: peiqzh/Auto-Renew-Katabump + liveqte/Auto-Renew-Katabump
核心: uc 绕过 Turnstile + Xvfb 有头模式 + Altcha 弹窗验证

流程:
1. uc.Chrome (HEADLESS=false + Xvfb) → 不被 Turnstile 检测
2. 填表 + ActionChains 偏移点击 Turnstile
3. 点击 See → 检查到期日 → Renew → Altcha checkbox → 提交
4. TG 通知结果
"""

import os, sys, time, logging, random, re, json, subprocess
from datetime import datetime, timezone, timedelta

import requests
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, WebDriverException

# ===================== 配置 =====================
HEADLESS = os.getenv('HEADLESS', 'false').lower() == 'true'
ACCOUNTS_ENV = os.getenv('ACCOUNTS', os.getenv('USERS_JSON', ''))
PROXY_SERVER = os.getenv('HTTP_PROXY', '')
HY2_URL = os.getenv('HY2_PROXY_URL', '')

TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN', os.getenv('BOT_TOKEN', ''))
TG_CHAT_ID = os.getenv('TG_CHAT_ID', os.getenv('CHAT_ID', ''))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def setup_hy2_proxy():
    """Start sing-box tunnel for HY2_PROXY_URL → socks5://127.0.0.1:10900.
    Returns the proxy string (or '' if not used/failed)."""
    import subprocess, tempfile, time as _time, json as _json, re as _re, urllib.parse as _up
    hy2 = os.getenv('HY2_PROXY_URL', '')
    if not hy2.startswith('hysteria2://'):
        return ''
    _cwd = os.getcwd()
    _sb = os.path.join(_cwd, 'sing-box')
    if not os.path.exists(_sb):
        _sb = subprocess.run(['which', 'sing-box'], capture_output=True, text=True).stdout.strip()
    if not _sb:
        logger.warning("⚠️ 未找到 sing-box 二进制，回退直连")
        return ''
    m = _re.match(r'hysteria2://([^@]+)@([^:]+):(\d+)\?([^#]*)', hy2)
    if not m:
        logger.warning("⚠️ HY2_URL 格式无法解析，回退直连")
        return ''
    pw, srv, port, qs = m.group(1), m.group(2), int(m.group(3)), m.group(4)
    qd = dict(_up.parse_qsl(qs))
    peer = qd.get('peer', 'www.bing.com')
    cfg = {'log': {'level': 'warn'},
           'inbounds': [{'type': 'socks', 'listen': '127.0.0.1', 'listen_port': 10900}],
           'outbounds': [{'type': 'hysteria2', 'server': srv, 'server_port': port,
                          'password': pw, 'tls': {'enabled': True, 'server_name': peer,
                                                 'insecure': True}}]}
    _cf = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
    _json.dump(cfg, _cf); _cf.close()
    try:
        subprocess.Popen([_sb, 'run', '-c', _cf.name],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _time.sleep(3)
        _ok = subprocess.run(['curl', '-s', '--max-time', '5', '-x', 'socks5://127.0.0.1:10900',
                              'https://api.ipify.org'], capture_output=True, text=True)
        if _ok.returncode == 0 and _ok.stdout.strip():
            logger.info(f"🛡️ HY2 代理已启动 → socks5://127.0.0.1:10900 (exit ip: {_ok.stdout.strip()})")
            return 'socks5://127.0.0.1:10900'
        else:
            logger.warning("⚠️ HY2 启动但代理不可用，回退直连")
    except Exception as e:
        logger.warning(f"HY2 启动失败，回退直连: {e}")
    return ''

# ===================== 工具 =====================
def rand_int(a, b): return random.randint(a, b)
def sleep_ms(ms): time.sleep(ms / 1000)
def human_delay(): sleep_ms(7000 + random.random() * 5000)

def human_type(driver, selector, text):
    try:
        el = WebDriverWait(driver, 15).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, selector)))
        el.clear()
        for ch in text:
            el.send_keys(ch)
            sleep_ms(rand_int(50, 150))
        return True
    except Exception as e:
        logger.warning(f"打字失败: {e}")
        return False

def mask_email(email):
    try:
        if '@' in email:
            p, d = email.split('@', 1)
            return f"{p[0]}***@{d}" if len(p) > 2 else f"{p}***@{d}"
        return f"{email[0]}***"
    except:
        return "User"

# ===================== TG 通知 =====================
REPO_NAME = os.getenv('GITHUB_REPOSITORY', 'btpp03/KataBump-AutoRenew')

def _proxy_label():
    hy2 = os.getenv('HY2_PROXY_URL', '')
    if hy2.startswith('hysteria2://'):
        # show exit ip hint from host part
        try:
            host = hy2.split('@')[1].split('?')[0].split(':')[0]
            return f"🛡️ 代理: HY2 住宅 ({host})"
        except Exception:
            return "🛡️ 代理: HY2 住宅"
    if PROXY_SERVER:
        return f"🛡️ 代理: {PROXY_SERVER}"
    return "🛡️ 代理: 直连 (无)"

def send_tg(text, photo_path=None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.warning("TG 未配置: BOT_TOKEN或CHAT_ID为空，跳过通知")
        return
    tz = timezone(timedelta(hours=8))
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    header = f"🔄 KataBump 续期通知\n📦 Repo: {REPO_NAME}\n{_proxy_label()}\n🕐 时间: {ts}"
    full = f"{header}\n\n{text}"
    try:
        if photo_path and os.path.exists(photo_path):
            resp = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TG_CHAT_ID, "caption": full},
                files={'photo': open(photo_path, 'rb')},
                timeout=20)
        else:
            resp = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                data={"chat_id": TG_CHAT_ID, "text": full},
                timeout=10)
        if resp.status_code != 200:
            logger.warning(f"TG 发送失败: HTTP {resp.status_code} {resp.text[:200]}")
        else:
            logger.info("✅ TG 通知已发送")
    except Exception as e:
        logger.warning(f"TG 发送失败: {e}")


# =# ===================== 核心 =====================
class KataBumpRenew:
    def __init__(self, user, password):
        self.user = user
        self.password = password
        self.masked = mask_email(user)
        self.driver = None
        self.screenshot_path = None

    def setup_driver(self):
        """每次调用都创建新的 Options (uc 不允许重用)"""
        opts = Options()
        if HEADLESS:
            opts.add_argument('--headless')
        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--disable-blink-features=AutomationControlled')
        opts.add_argument('--remote-debugging-port=9222')
        if PROXY_SERVER:
            opts.add_argument(f'--proxy-server={PROXY_SERVER}')

        v_env = os.getenv('CHROME_VERSION', '')
        v_main = int(v_env) if v_env.isdigit() else None
        logger.info(f"🛠️ 驱动初始化 - 版本: {v_main or '自动'}")

        for v in [v_main, None]:
            try:
                self.driver = uc.Chrome(options=opts, headless=HEADLESS,
                                        version_main=v, use_subprocess=True)
                self.driver.set_window_size(1280, 720)
                return
            except Exception as e:
                if self.driver:
                    try: self.driver.quit()
                    except: pass
                    self.driver = None
                # uc 不允许重用 Options，重试时创建新的
                opts = Options()
                if HEADLESS:
                    opts.add_argument('--headless')
                opts.add_argument('--no-sandbox')
                opts.add_argument('--disable-dev-shm-usage')
                opts.add_argument('--disable-blink-features=AutomationControlled')
                opts.add_argument('--remote-debugging-port=9222')
                if PROXY_SERVER:
                    opts.add_argument(f'--proxy-server={PROXY_SERVER}')
                if v is None:
                    raise

    def _click_turnstile_iframe(self):
        """Turnstile 真实 checkbox 在 iframe 内 — 切进去点真实 checkbox"""
        try:
            # find the turnstile iframe
            target = None
            # 1) src-based detection
            for f in self.driver.find_elements(By.TAG_NAME, "iframe"):
                src = f.get_attribute("src") or ""
                cls = f.get_attribute("class") or ""
                if "turnstile" in src or "chl-widget" in cls or "challenges" in src:
                    target = f
                    break
            # 2) fallback: any visible sized iframe next to cf-turnstile
            if not target:
                try:
                    host = self.driver.find_element(By.CLASS_NAME, "cf-turnstile")
                    # the iframe is usually a sibling/parent
                    for f in self.driver.find_elements(By.TAG_NAME, "iframe"):
                        box = f.size
                        if box.get('width', 0) > 30 and box.get('height', 0) > 30:
                            target = f
                            break
                except Exception:
                    pass
            if not target:
                return False

            self.driver.switch_to.frame(target)
            try:
                cb = WebDriverWait(self.driver, 8).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR,
                        "input[type='checkbox']")))
                actions = ActionChains(self.driver)
                actions.move_to_element(cb)
                actions.pause(random.uniform(0.3, 0.6))
                actions.click()
                actions.perform()
                logger.info(f"🖱️ {self.masked} Turnstile iframe checkbox 点击")
                return True
            finally:
                self.driver.switch_to.default_content()
        except Exception as e:
            try:
                self.driver.switch_to.default_content()
            except Exception:
                pass
            logger.warning(f"⚠️ {self.masked} Turnstile iframe 点击失败: {e}")
            return False

    def _handle_turnstile(self, context=""):
        """Cloudflare Turnstile — 先点 iframe 内真实 checkbox，再等待 token 出现"""
        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CLASS_NAME, "cf-turnstile")))

            clicked = self._click_turnstile_iframe()
            if not clicked:
                # fallback: offset click on the container (old method)
                try:
                    container = self.driver.find_element(By.CLASS_NAME, "cf-turnstile")
                    size = container.size
                    rand_x = -(size['width'] / 2) + (size['width'] * 0.12) + random.uniform(-5, 5)
                    rand_y = random.uniform(-5, 5)
                    actions = ActionChains(self.driver)
                    actions.move_to_element(container)
                    actions.pause(random.uniform(0.5, 0.8))
                    actions.move_to_element_with_offset(container, rand_x, rand_y)
                    actions.click_and_hold()
                    actions.pause(random.uniform(0.1, 0.25))
                    actions.release()
                    actions.perform()
                    logger.info(f"🖱️ {self.masked} [{context}] Turnstile 偏移点击 (fallback)")
                except Exception as e:
                    logger.warning(f"⚠️ {self.masked} [{context}] 偏移点击异常: {e}")

            # 轮询 token (CF 有时延迟几秒才返回)
            for _ in range(30):
                try:
                    token = self.driver.execute_script(
                        'return document.querySelector("input[name=\'cf-turnstile-response\']").value;')
                except Exception:
                    token = None
                if token and len(str(token)) > 20:
                    logger.info(f"✅ {self.masked} [{context}] Turnstile 通过!")
                    sleep_ms(1500 + random.random() * 1000)
                    return True
                sleep_ms(1000)
            logger.warning(f"⚠️ {self.masked} [{context}] Turnstile 超时")
            return False
        except Exception as e:
            logger.error(f"❌ {self.masked} [{context}] Turnstile 失败: {e}")
            return False

    def _handle_altcha(self):
        """续期弹窗的 Altcha 验证 — checkbox click"""
        try:
            checkbox = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//div[@class='altcha']//input[@type='checkbox' and @required]")))
            logger.info(f"✅ {self.masked} 找到 Altcha 复选框")
            checkbox.click()
            sleep_ms(8000 + random.random() * 2000)
        except TimeoutException:
            logger.warning("⚠️ 未找到 Altcha 复选框 (可能不需要)")

    def process(self):
        """主续期流程"""
        logger.info(f"🚀 登录: {self.masked}")
        self.driver.get("https://dashboard.katabump.com/auth/login")
        sleep_ms(5000 + random.random() * 2000)

        # 填表
        logger.info(f"📝 {self.masked} 填写邮箱...")
        if not human_type(self.driver, "input#email", self.user):
            raise Exception("未找到邮箱输入框")
        sleep_ms(2000 + random.random() * 1000)

        logger.info(f"🔒 {self.masked} 填写密码...")
        if not human_type(self.driver, "input#password", self.password):
            raise Exception("未找到密码输入框")
        sleep_ms(2000 + random.random() * 1000)

        # Turnstile
        self._handle_turnstile("Login")

        # 登录
        logger.info(f"📤 {self.masked} 提交登录...")
        self.driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
        human_delay()

        # Debug: screenshot + page info after login attempt
        try:
            ss_path = f"debug-login-{self.user.split('@')[0]}.png"
            self.driver.save_screenshot(ss_path)
            logger.info(f"📸 登录后截图: {ss_path}")
            error_els = self.driver.find_elements(By.CSS_SELECTOR, '.error, .alert-danger, .invalid-feedback, [role=alert], .toast-error, .text-red, .text-danger')
            for el in error_els:
                txt = el.text.strip()
                if txt:
                    logger.info(f"⚠️ 页面错误: {txt}")
            logger.info(f"📍 URL: {self.driver.current_url}")
            logger.info(f"📍 Title: {self.driver.title}")
        except Exception as e:
            logger.warning(f"截图失败: {e}")

        # 检查是否还在登录页
        if "login" in self.driver.current_url:
            raise Exception("登录失败 — 仍在登录页")

        # 进入服务器详情
        logger.info(f"🎯 {self.masked} 进入服务器页...")
        manage_btn = WebDriverWait(self.driver, 30).until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'See')]")))
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", manage_btn)
        sleep_ms(1000 + random.random() * 1000)
        self.driver.execute_script("arguments[0].click();", manage_btn)
        human_delay()

        # 检查到期日
        logger.info(f"📅 {self.masked} 检查到期日...")
        try:
            expiry_el = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//div[contains(text(), 'Expiry')]/following-sibling::div")))
            expiry_text = expiry_el.text.strip()
            logger.info(f"⌛ {self.masked} 到期: {expiry_text}")

            tz_hkt = timezone(timedelta(hours=8))
            today = datetime.now(tz_hkt).date()
            expiry_date = None
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"]:
                try:
                    expiry_date = datetime.strptime(expiry_text, fmt).date()
                    break
                except ValueError:
                    continue

            if expiry_date:
                days_diff = (expiry_date - today).days
                if days_diff > 1:
                    notice = f"⏰ {self.masked}\n📅 未到续期日: {expiry_text}\n🔄 剩余 {days_diff} 天"
                    logger.info(f"ℹ️ {notice}")
                    return True, notice
                elif days_diff < 0:
                    notice = f"⚠️ {self.masked}\n📅 已过期 {abs(days_diff)} 天: {expiry_text}\n⚠️ 可能已被删除!"
                    logger.warning(notice)
                    return False, notice
        except Exception as e:
            logger.warning(f"⚠️ 日期检查异常: {e}，继续续期")

        # 点击 Renew
        logger.info(f"🔄 {self.masked} 续期流程...")
        try:
            renew_btn = WebDriverWait(self.driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Renew')]")))
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", renew_btn)
            self.driver.execute_script("arguments[0].click();", renew_btn)
            logger.info(f"📑 {self.masked} 打开 Renew 弹窗")
        except Exception as e:
            raise Exception(f"无法打开 Renew 弹窗: {e}")

        sleep_ms(2000 + random.random() * 1000)

        # Altcha
        self._handle_altcha()

        # 最终 Renew
        try:
            confirm = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//div[@id='renew-modal']//button[@type='submit' and contains(text(), 'Renew')]")))
            self.driver.execute_script("arguments[0].click();", confirm)
        except Exception as e:
            raise Exception(f"弹窗提交失败: {e}")

        sleep_ms(7000 + random.random() * 2000)

        # 结果核验
        try:
            alerts = self.driver.find_elements(By.CSS_SELECTOR, ".alert-danger")
            if alerts and alerts[0].is_displayed():
                msg = alerts[0].text.strip().replace('×', '')
                return False, f"⚠️ {self.masked}\n续期失败: {msg}"

            final_el = self.driver.find_element(
                By.XPATH, "//div[contains(text(), 'Expiry')]/following-sibling::div")
            final = final_el.text.strip()
            logger.info(f"✅ {self.masked} 续期后到期: {final}")
            if final and final != expiry_text:
                return True, f"✅ {self.masked}\n🎉 续期成功!\n📅 新到期: {final}"
            else:
                return False, f"⚠️ {self.masked}\n时间未更新 ({final})"
        except Exception as e:
            return False, f"❌ {self.masked}\n验证结果异常: {e}"

    def run(self):
        max_retries = 3
        last_error = ""
        for attempt in range(max_retries):
            try:
                if not self.driver:
                    self.setup_driver()
                if attempt > 0:
                    logger.info(f"🔄 {self.masked} 第 {attempt+1} 次尝试...")
                    # 关闭旧 driver，重新创建
                    try: self.driver.quit()
                    except: pass
                    self.driver = None
                    self.setup_driver()
                    self.driver.get("https://dashboard.katabump.com/auth/login")
                    sleep_ms(5000 + random.random() * 3000)
                success, msg = self.process()
                if success:
                    return True, msg
                last_error = msg
                if "续期失败" in msg or "已过期" in msg:
                    break
            except Exception as e:
                last_error = str(e)[:80]
                logger.error(f"❌ {self.masked} 第 {attempt+1} 次: {e}")
                if self.driver:
                    try: self.driver.quit()
                    except: pass
                    self.driver = None
                if attempt < max_retries - 1:
                    sleep_ms(5000 + random.random() * 5000)

        self.screenshot_path = f"error-{self.user.split('@')[0]}.png"
        if self.driver:
            self.driver.save_screenshot(self.screenshot_path)
        return False, f"❌ {self.masked}\n{max_retries} 次尝试均失败\n{last_error}"


# ===================== 多账号 =====================
def load_accounts():
    """解析账号: 格式 user:pass,user:pass 或 JSON"""
    accounts = []
    if not ACCOUNTS_ENV:
        return accounts

    # 尝试 JSON 格式
    try:
        users = json.loads(ACCOUNTS_ENV)
        if isinstance(users, list):
            for u in users:
                accounts.append({
                    'user': u.get('email', u.get('username', u.get('user', ''))),
                    'pass': u.get('password', u.get('pass', ''))
                })
            return accounts
    except:
        pass

    # user:pass,user:pass 格式
    for a in re.split(r'[,;\n]', ACCOUNTS_ENV):
        a = a.strip()
        if ':' in a:
            u, p = a.split(':', 1)
            accounts.append({'user': u.strip(), 'pass': p.strip()})

    return accounts


def main():
    logger.info("=" * 50)
    logger.info("🚀 KataBump 自动续期启动！")
    logger.info("=" * 50)

    global PROXY_SERVER
    hy2_proxy = setup_hy2_proxy()
    if hy2_proxy:
        PROXY_SERVER = hy2_proxy

    accounts = load_accounts()
    if not accounts:
        logger.error("❌ 未配置账号")
        send_tg("❌ KataBump 续期失败\n未配置账号")
        sys.exit(1)

    logger.info(f"📋 共 {len(accounts)} 个账号")
    results = []
    success_count = 0

    for i, acc in enumerate(accounts):
        logger.info(f"\n{'='*30}\n📋 第 {i+1}/{len(accounts)} 个账号")
        bot = KataBumpRenew(acc['user'], acc['pass'])
        success, msg = bot.run()
        results.append({'msg': msg, 'ok': success})
        if success:
            success_count += 1

        if bot.driver:
            try:
                bot.driver.quit()
            except:
                pass
            bot.driver = None

        if i < len(accounts) - 1:
            wait = 10000 + random.random() * 5000
            logger.info(f"⏳ 等待 {wait/1000:.0f}s...")
            sleep_ms(wait)

    # 汇总
    header = f"📦 Repo: {REPO_NAME}\n{_proxy_label()}\n📊 续期汇总: {success_count}/{len(accounts)} 成功"
    summary = header + "\n\n"
    summary += "\n\n".join([r['msg'] for r in results])
    logger.info(summary)
    send_tg(summary)

    sys.exit(0 if success_count == len(accounts) else 1)


if __name__ == "__main__":
    main()

