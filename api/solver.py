<?php

declare(strict_types=1);

/**
 * hCaptcha browser solver — PHP port using Chrome DevTools Protocol.
 *
 * Opens a local demo page, runs the widget, intercepts
 * api.hcaptcha.com/getcaptcha/{sitekey}, returns token + raw API body.
 *
 * Default sitekey matches nw.php / hcaptcha_lib.php.
 *
 * Install:
 *   composer install
 *   # Ensure Chrome/Chromium is installed and accessible
 */

namespace HcaptchaSolver;

use HeadlessChromium\BrowserFactory;
use HeadlessChromium\Browser;
use HeadlessChromium\Page;
use HeadlessChromium\PageNavigation;
use HeadlessChromium\Communication\Message;

// ─── Constants ────────────────────────────────────────────────────────
const DEFAULT_SITEKEY  = '12baaa15-55cb-409a-bbbe-900132d52afa';
const DEFAULT_HOST     = 'www.epassport.gov.bd';
const DEFAULT_PAGE_URL = 'https://www.epassport.gov.bd/';
const GETCAPTCHA_BASE  = 'https://api.hcaptcha.com/getcaptcha/';
const TOKEN_TTL        = 110;

const IS_LINUX = PHP_OS_FAMILY === 'Linux';

const USER_AGENT = IS_LINUX
    ? 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
    : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

// ─── Token cache (file-based, persists across requests) ──────────────

function cache_dir(): string
{
    $dir = sys_get_temp_dir() . '/hcaptcha_tokens';
    if (!is_dir($dir)) {
        @mkdir($dir, 0755, true);
    }
    return $dir;
}

// ─── Helper functions ─────────────────────────────────────────────────

function env_bool(string $name, bool $default): bool
{
    $val = getenv($name);
    if ($val === false) {
        return $default;
    }
    return in_array(strtolower(trim($val)), ['1', 'true', 'yes', 'on'], true);
}

function default_headless(): bool
{
    $default = IS_LINUX && !getenv('DISPLAY');
    return env_bool('HEADLESS', $default);
}

function free_port(): int
{
    $sock = socket_create(AF_INET, SOCK_STREAM, 0);
    socket_bind($sock, '127.0.0.1', 0);
    $name = '';
    $port = 0;
    socket_getsockname($sock, $name, $port);
    socket_close($sock);
    return $port;
}

// ─── Demo HTML ────────────────────────────────────────────────────────

function demo_html(string $sitekey, string $host): string
{
    $sk = json_encode($sitekey);
    $h  = json_encode($host);

    return <<<HTML
<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>hCaptcha demo</title>
<script src="https://js.hcaptcha.com/1/api.js?render=explicit" async defer></script>
<style>body{font-family:system-ui;margin:24px;background:#f4f4f8}
#hcaptcha-container{min-height:78px}</style>
</head><body>
<h3>hCaptcha demo</h3>
<div id="hcaptcha-container"></div>
<pre id="out" style="font-size:11px;word-break:break-all"></pre>
<script>
const SITEKEY = {$sk};
const HOST = {$h};
let widgetId = null;
window.__token = null;
window.__getcaptcha = null;

function setOut(msg) {
  const el = document.getElementById('out');
  if (el) el.textContent = typeof msg === 'string' ? msg : JSON.stringify(msg, null, 2);
}

function onCaptchaSuccess(token) {
  window.__token = token;
  setOut({ source: 'hcaptcha_callback', token_preview: token.slice(0, 80) + '...' });
}

function onCaptchaExpired() {
  setTimeout(() => {
    if (widgetId !== null && typeof hcaptcha !== 'undefined') hcaptcha.execute(widgetId);
  }, 1500);
}

function onCaptchaError(err) {
  window.__error = String(err);
  setOut({ error: window.__error });
}

function initWidget() {
  if (typeof hcaptcha === 'undefined' || widgetId) return;
  const opts = {
    sitekey: SITEKEY,
    callback: onCaptchaSuccess,
    'expired-callback': onCaptchaExpired,
    'error-callback': onCaptchaError
  };
  if (HOST) opts.host = HOST;
  widgetId = hcaptcha.render('hcaptcha-container', opts);
  window.__widgetReady = true;
}

window.addEventListener('load', () => {
  const tick = setInterval(() => {
    if (typeof hcaptcha !== 'undefined') {
      clearInterval(tick);
      initWidget();
    }
  }, 80);
});

const GETCAPTCHA_MATCH = 'api.hcaptcha.com/getcaptcha/';

function captureGetCaptcha(bodyText, url) {
  window.__getcaptcha = { url, body: bodyText, at: Date.now() };
  try {
    window.__getcaptcha.json = JSON.parse(bodyText);
  } catch (e) {}
}

(function installInterceptors() {
  const origFetch = window.fetch;
  window.fetch = async function(...args) {
    const res = await origFetch.apply(this, args);
    try {
      const req = args[0];
      const url = typeof req === 'string' ? req : (req?.url || '');
      if (url.includes(GETCAPTCHA_MATCH)) {
        const clone = res.clone();
        captureGetCaptcha(await clone.text(), url);
      }
    } catch (e) {}
    return res;
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this._interceptUrl = url;
    return origOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...args) {
    this.addEventListener('load', function() {
      const url = this._interceptUrl || '';
      if (url.includes(GETCAPTCHA_MATCH)) {
        captureGetCaptcha(this.responseText, url);
      }
    });
    return origSend.apply(this, args);
  };
})();

window.runSolve = function() {
  return new Promise((resolve, reject) => {
    if (widgetId === null || typeof hcaptcha === 'undefined') {
      reject(new Error('hcaptcha not ready'));
      return;
    }
    hcaptcha.reset(widgetId);
    setTimeout(() => {
      try { hcaptcha.execute(widgetId); resolve(true); }
      catch (e) { reject(e); }
    }, 400);
  });
};
</script></body></html>
HTML;
}

// ─── Token extraction ─────────────────────────────────────────────────

function extract_token(?string $body): ?string
{
    if ($body === null || $body === '') {
        return null;
    }
    $text = trim($body);

    $data = json_decode($text, true);
    if (!is_array($data)) {
        if (strlen($text) > 80 && !str_starts_with($text, '<')) {
            return $text;
        }
        return null;
    }

    foreach (['generated_pass_UUID', 'pass', 'key', 'token', 'response'] as $key) {
        if (isset($data[$key]) && is_string($data[$key]) && strlen($data[$key]) > 80) {
            return $data[$key];
        }
    }

    if (isset($data['c']) && is_array($data['c'])) {
        $t = $data['c']['token'] ?? null;
        if (is_string($t) && strlen($t) > 80) {
            return $t;
        }
    }

    return null;
}

// ─── Cache ────────────────────────────────────────────────────────────

function cache_get(string $sitekey): ?array
{
    $file = cache_dir() . '/' . md5($sitekey) . '.json';
    if (!is_file($file)) {
        return null;
    }
    $raw = @file_get_contents($file);
    if ($raw === false) {
        return null;
    }
    $entry = json_decode($raw, true);
    if (!is_array($entry)) {
        @unlink($file);
        return null;
    }
    if (time() >= ($entry['expires_at'] ?? 0)) {
        @unlink($file);
        return null;
    }
    return $entry;
}

function cache_put(string $sitekey, array $payload): void
{
    $payload['expires_at'] = time() + TOKEN_TTL;
    $payload['cached_at']  = time();
    $file = cache_dir() . '/' . md5($sitekey) . '.json';
    file_put_contents($file . '.tmp', json_encode($payload));
    rename($file . '.tmp', $file);
}

function get_cached_token(string $sitekey = DEFAULT_SITEKEY): ?string
{
    $entry = cache_get($sitekey);
    return $entry['token'] ?? null;
}

function clear_cache(?string $sitekey = null): void
{
    if ($sitekey !== null) {
        $file = cache_dir() . '/' . md5($sitekey) . '.json';
        @unlink($file);
    } else {
        foreach (glob(cache_dir() . '/*.json') ?: [] as $file) {
            @unlink($file);
        }
    }
}

// ─── SolveResult ──────────────────────────────────────────────────────

class SolveResult
{
    public bool   $success;
    public string $sitekey;
    public ?string $token             = null;
    public ?string $source            = null;
    public string  $getcaptcha_url    = '';
    public mixed   $getcaptcha_body   = null;
    public ?string $getcaptcha_raw    = null;
    public ?string $error             = null;
    public float   $took_ms           = 0.0;
    public array   $extra             = [];

    public function __construct(array $args = [])
    {
        foreach ($args as $k => $v) {
            $this->$k = $v;
        }
    }

    public function to_dict(): array
    {
        $rawPreview = null;
        if ($this->getcaptcha_raw !== null) {
            $rawPreview = substr($this->getcaptcha_raw, 0, 500);
        }

        return array_merge([
            'success'                  => $this->success,
            'sitekey'                  => $this->sitekey,
            'token'                    => $this->token,
            'source'                   => $this->source,
            'getcaptcha_url'           => $this->getcaptcha_url ?: (GETCAPTCHA_BASE . $this->sitekey),
            'getcaptcha'               => $this->getcaptcha_body,
            'getcaptcha_raw_preview'   => $rawPreview,
            'error'                    => $this->error,
            'took_ms'                  => round($this->took_ms, 1),
        ], $this->extra);
    }
}

// ─── Find Chrome/Chromium binary ──────────────────────────────────────

function find_chrome_path(): ?string
{
    $envPath = getenv('HCAPTCHA_CHROME');
    if ($envPath !== false && $envPath !== '') {
        if (file_exists($envPath) && is_executable($envPath)) {
            return $envPath;
        }
    }

    $candidates = [
        // Termux — actual binaries (not symlinks)
        '/data/data/com.termux/files/usr/lib/chromium/chrome',
        '/data/data/com.termux/files/usr/lib/chromium/headless_shell',
        '/data/data/com.termux/files/usr/bin/chromium-browser',
        // Standard Linux
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
        '/snap/bin/chromium',
        // macOS
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        // Windows
        'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
        'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    ];

    foreach ($candidates as $path) {
        if (file_exists($path) && is_executable($path)) {
            return $path;
        }
    }

    // Fallback: find real binary behind symlinks
    foreach ([
        '/data/data/com.termux/files/usr/lib/chromium',
        '/data/data/com.termux/files/usr/bin',
    ] as $dir) {
        if (is_dir($dir)) {
            foreach (scandir($dir) ?: [] as $entry) {
                if ($entry === '.' || $entry === '..') continue;
                $full = $dir . '/' . $entry;
                if (is_executable($full) && preg_match('/chrome|chromium|headless_shell/i', $entry)) {
                    return $full;
                }
            }
        }
    }

    return null;
}

// ─── Browser launch arguments ─────────────────────────────────────────

function browser_launch_args(bool $headless): array
{
    $args = [
        '--disable-blink-features=AutomationControlled',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-extensions',
        '--disable-background-networking',
        '--mute-audio',
        '--window-size=1280,800',
    ];

    if ($headless) {
        $args[] = '--headless=new';
    }

    if (IS_LINUX || env_bool('CHROMIUM_NO_SANDBOX', false)) {
        array_push($args,
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-software-rasterizer',
        );
    }

    return $args;
}

// ─── Main solver ──────────────────────────────────────────────────────

function solve_hcaptcha(
    string $sitekey = DEFAULT_SITEKEY,
    array $options = []
): SolveResult {
    $host        = $options['host']        ?? DEFAULT_HOST;
    $page_url    = $options['page_url']    ?? DEFAULT_PAGE_URL;
    $headless    = $options['headless']    ?? default_headless();
    $timeout_sec = $options['timeout_sec'] ?? 90.0;
    $force       = $options['force']       ?? false;

    // Check cache
    if (!$force) {
        $cached = cache_get($sitekey);
        if ($cached && isset($cached['token']) && $cached['token'] !== null) {
            return new SolveResult([
                'success'         => true,
                'sitekey'         => $sitekey,
                'token'           => $cached['token'],
                'source'          => 'cache',
                'getcaptcha_url'  => $cached['getcaptcha_url'] ?? (GETCAPTCHA_BASE . $sitekey),
                'getcaptcha_body' => $cached['getcaptcha_body'] ?? null,
                'took_ms'         => 0.0,
                'extra'           => ['from_cache' => true],
            ]);
        }
    }

    $t0 = microtime(true);
    $getcaptcha_url = GETCAPTCHA_BASE . $sitekey;

    // Find Chrome
    $chromePath = find_chrome_path();
    if ($chromePath === null) {
        return new SolveResult([
            'success'  => false,
            'sitekey'  => $sitekey,
            'error'    => 'Chrome/Chromium not found — install chromium',
            'took_ms'  => (microtime(true) - $t0) * 1000,
        ]);
    }

    // Start a temporary built-in PHP server for the demo page
    $port   = free_port();
    $html   = demo_html($sitekey, $host);
    $tmpDir = sys_get_temp_dir() . '/hcaptcha_' . md5($sitekey . $port);
    if (!is_dir($tmpDir)) {
        mkdir($tmpDir, 0755, true);
    }
    file_put_contents($tmpDir . '/index.html', $html);

    $serverProc = proc_open(
        PHP_BINARY . " -S 127.0.0.1:{$port} -t " . escapeshellarg($tmpDir),
        [
            0 => ['pipe', 'r'],
            1 => ['pipe', 'w'],
            2 => ['pipe', 'w'],
        ],
        $pipes
    );

    if (!is_resource($serverProc)) {
        return new SolveResult([
            'success' => false,
            'sitekey' => $sitekey,
            'error'   => 'Failed to start local demo server',
            'took_ms' => (microtime(true) - $t0) * 1000,
        ]);
    }

    // Wait for server to start
    usleep(300_000); // 300ms
    $demoUrl = "http://127.0.0.1:{$port}/";

    $captured = ['body' => null, 'raw' => null, 'url' => null, 'requestId' => null];
    $tokenHolder = ['token' => null, 'source' => null];

    try {
        // Create browser factory
        $factory = new BrowserFactory($chromePath);
        $browserArgs = browser_launch_args($headless);

        $browser = $factory->createBrowser([
            'headless'           => $headless,
            'noDefaultArgs'      => true,
            'customFlags'        => $browserArgs,
            'userAgent'          => USER_AGENT,
            'windowSize'         => [1280, 800],
            'keepAlive'          => false,
        ]);

        try {
            $page = $browser->createPage();
            $session = $page->getSession();

            // Stealth: remove webdriver property
            $page->evaluate('Object.defineProperty(navigator, "webdriver", { get: () => undefined })')->getReturnValue();

            // Network interception: record getcaptcha responses (requestId for Network.getResponseBody)
            $session->on('method:Network.responseReceived', function (array $params) use (&$captured, $sitekey): void {
                $url = $params['response']['url'] ?? '';
                if (strpos($url, 'api.hcaptcha.com/getcaptcha/') === false || strpos($url, $sitekey) === false) {
                    return;
                }
                $captured['url']       = $url;
                $captured['requestId'] = $params['requestId'] ?? null;
            });

            $page->navigate($demoUrl)->waitForNavigation(Page::DOM_CONTENT_LOADED, 30000);
            usleep(500_000); // wait for page load

            // Wait for widget to be ready
            $deadline = microtime(true) + 45;
            while (microtime(true) < $deadline) {
                $ready = $page->evaluate('window.__widgetReady === true')->getReturnValue();
                if ($ready === true) {
                    break;
                }
                usleep(100_000); // 100ms
            }

            // Execute the solve
            $page->evaluate('window.runSolve()')->getReturnValue();
            usleep(800_000); // let it execute

            // Poll for token
            $deadline = microtime(true) + $timeout_sec;
            $pageErr = null;

            while (microtime(true) < $deadline) {
                // Check callback token
                $cbToken = $page->evaluate('window.__token')->getReturnValue();
                if (is_string($cbToken) && strlen($cbToken) > 80) {
                    $tokenHolder['token']  = $cbToken;
                    $tokenHolder['source'] = $tokenHolder['source'] ?? 'hcaptcha_callback';
                }

                if ($tokenHolder['token'] !== null) {
                    break;
                }

                // Check for errors
                $err = $page->evaluate('window.__error')->getReturnValue();
                if (is_string($err) && $err !== '') {
                    $pageErr = $err;
                }

                usleep(250_000); // 250ms
            }

            // Check window.__getcaptcha from JS
            $gc = $page->evaluate('window.__getcaptcha')->getReturnValue();
            if (is_array($gc)) {
                if ($captured['url'] === null && isset($gc['url'])) {
                    $captured['url'] = $gc['url'];
                }
                $bodyText = $gc['body'] ?? null;
                if ($bodyText !== null && $captured['raw'] === null) {
                    $captured['raw'] = $bodyText;
                    if (isset($gc['json'])) {
                        $captured['body'] = $gc['json'];
                    } else {
                        $decoded = json_decode($bodyText, true);
                        $captured['body'] = is_array($decoded) ? $decoded : $bodyText;
                    }
                    $tok = extract_token((string)$bodyText);
                    if ($tok !== null && $tokenHolder['token'] === null) {
                        $tokenHolder['token']  = $tok;
                        $tokenHolder['source'] = 'getcaptcha_api';
                    }
                }
            }

            // Also fetch captured network response body via CDP
            if ($captured['requestId'] !== null && $captured['raw'] === null) {
                try {
                    $resp = $session->sendMessageSync(
                        new Message('Network.getResponseBody', ['requestId' => $captured['requestId']]),
                        5000
                    );
                    $body = $resp->getResultData('body');
                    if (is_string($body)) {
                        $captured['raw'] = $body;
                        $decoded = json_decode($body, true);
                        $captured['body'] = is_array($decoded) ? $decoded : $body;
                        $tok = extract_token($body);
                        if ($tok !== null && $tokenHolder['token'] === null) {
                            $tokenHolder['token']  = $tok;
                            $tokenHolder['source'] = 'getcaptcha_api';
                        }
                    }
                } catch (\Throwable $e) {
                    // ignore
                }
            }

        } finally {
            $browser->close();
        }
    } catch (\Throwable $e) {
        return new SolveResult([
            'success'         => false,
            'sitekey'         => $sitekey,
            'error'           => $e->getMessage(),
            'getcaptcha_url'  => $getcaptcha_url,
            'getcaptcha_body' => $captured['body'],
            'getcaptcha_raw'  => $captured['raw'],
            'took_ms'         => (microtime(true) - $t0) * 1000,
        ]);
    } finally {
        // Kill the temp server
        if (is_resource($serverProc)) {
            proc_terminate($serverProc, 9);
            proc_close($serverProc);
        }
        // Cleanup temp files
        @unlink($tmpDir . '/index.html');
        @rmdir($tmpDir);
    }

    $took = (microtime(true) - $t0) * 1000;
    $token = $tokenHolder['token'];

    if ($token === null && $captured['raw'] !== null) {
        $token = extract_token((string)$captured['raw']);
    }

    if ($token === null) {
        return new SolveResult([
            'success'         => false,
            'sitekey'         => $sitekey,
            'error'           => $pageErr ?? 'timeout — no token (HSW/image challenge may need visible browser)',
            'getcaptcha_url'  => $captured['url'] ?? $getcaptcha_url,
            'getcaptcha_body' => $captured['body'],
            'getcaptcha_raw'  => $captured['raw'],
            'took_ms'         => $took,
        ]);
    }

    $result = new SolveResult([
        'success'         => true,
        'sitekey'         => $sitekey,
        'token'           => $token,
        'source'          => $tokenHolder['source'],
        'getcaptcha_url'  => $captured['url'] ?? $getcaptcha_url,
        'getcaptcha_body' => $captured['body'],
        'getcaptcha_raw'  => $captured['raw'],
        'took_ms'         => $took,
    ]);

    cache_put($sitekey, [
        'token'           => $token,
        'source'          => $result->source,
        'getcaptcha_url'  => $result->getcaptcha_url,
        'getcaptcha_body' => $result->getcaptcha_body,
    ]);

    return $result;
}
