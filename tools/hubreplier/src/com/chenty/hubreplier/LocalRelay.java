package com.chenty.hubreplier;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;

/**
 * 本机中转：127.0.0.1:&lt;relay_port&gt; 的 TCP 流量 → 经手机 MCP 的用户态隧道（SOCKS5 127.0.0.1:&lt;socks_port&gt;）→ info-hub。
 *
 * 为什么需要：手机不接 WireGuard 时 <hub-host> 不可达；com.jxev.ai 的隧道把 内网网段 送到公网 TCP/443，
 * 并在本机留了一个 SOCKS5 口（mcp_prefs.xml: tunnel_socks_port=10800）。App 只需连本机中转，
 * 由中转替它做 SOCKS5 握手，WebView 与 HomeSync 就都不再依赖 VPN。
 *
 * 契约见 INTERFACE.md §11.10（本机中转口 / 回退规则）。
 */
final class LocalRelay {
    static final String TAG = "HubReplier";
    static final String KEY_SOCKS = "socks_port";
    static final String KEY_RELAY_PORT = "relay_port";
    static final String KEY_RELAY_URL = "relay_url";  // 中转的目标；缺省用 hub_home.url
    private static final int DEFAULT_SOCKS = 10800;
    private static final int DEFAULT_RELAY_PORT = 8801;

    private static ServerSocket server;
    private static String targetHost = "";
    private static int targetPort = 80;
    private static int socksPort = DEFAULT_SOCKS;

    private LocalRelay() {}

    static int relayPort(Context ctx) {
        return prefs(ctx).getInt(KEY_RELAY_PORT, DEFAULT_RELAY_PORT);
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(HomeActivity.PREFS, Context.MODE_PRIVATE);
    }

    /** 本机入口 URL：WebView 与 HomeSync 都只用这个，目标 host:port 从 hub_home.url 解析。 */
    static String base(Context ctx) {
        return "http://127.0.0.1:" + relayPort(ctx) + "/";
    }

    /** 幂等：端口+目标不变就不重起；返回是否可用。 */
    static synchronized boolean ensure(Context ctx) {
        SharedPreferences sp = prefs(ctx);
        String url = sp.getString(KEY_RELAY_URL, "");
        if (url.isEmpty()) {
            url = sp.getString(HomeActivity.KEY_URL, "");
        }
        return ensureTarget(ctx, url);
    }

    /** 按给定目标起中转并返回本机入口；设置页「测试连接」用（不必先保存）。失败返回 null。 */
    static synchronized String baseFor(Context ctx, String targetUrl) {
        if (targetUrl == null || targetUrl.trim().isEmpty()) {
            return null;
        }
        return ensureTarget(ctx, targetUrl.trim()) ? base(ctx) : null;
    }

    private static synchronized boolean ensureTarget(Context ctx, String url) {
        SharedPreferences sp = prefs(ctx);
        if (url == null || url.isEmpty()) {
            return false;
        }
        String host;
        int port;
        try {
            URI u = URI.create(url);
            host = u.getHost();
            port = u.getPort() > 0 ? u.getPort() : 80;
        } catch (Exception e) {
            Log.w(TAG, "relay: bad url " + url + " (" + e + ")");
            return false;
        }
        if (host == null || host.isEmpty()) {
            return false;
        }
        int socks = sp.getInt(KEY_SOCKS, DEFAULT_SOCKS);
        int rport = relayPort(ctx);
        if (server != null && !server.isClosed() && host.equals(targetHost)
                && port == targetPort && socks == socksPort) {
            return true;
        }
        stop();
        closeQuietly(server);
        targetHost = host;
        targetPort = port;
        socksPort = socks;
        try {
            server = new ServerSocket();
            server.setReuseAddress(true);
            server.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), rport), 8);
        } catch (IOException e) {
            Log.w(TAG, "relay bind 127.0.0.1:" + rport + " failed: " + e);
            server = null;
            return false;
        }
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                acceptLoop();
            }
        }, "hub-relay");
        t.setDaemon(true);
        t.start();
        Log.i(TAG, "relay 127.0.0.1:" + rport + " -> " + host + ":" + port + " via socks 127.0.0.1:" + socks);
        return true;
    }

    static synchronized void stop() {
        ServerSocket s = server;
        server = null;
        closeQuietly(s);
    }

    private static void acceptLoop() {
        while (true) {
            ServerSocket s = server;
            if (s == null || s.isClosed()) {
                return;
            }
            final Socket client;
            try {
                client = s.accept();
            } catch (IOException e) {
                Log.i(TAG, "relay accept ended: " + e);
                return;
            }
            Thread t = new Thread(new Runnable() {
                @Override
                public void run() {
                    serve(client);
                }
            }, "hub-relay-conn");
            t.setDaemon(true);
            t.start();
        }
    }

    private static void serve(Socket client) {
        Socket upstream = null;
        try {
            client.setTcpNoDelay(true);
            upstream = openUpstream();
            if (upstream == null) {
                Log.w(TAG, "relay: 上游不可达 " + targetHost + ":" + targetPort);
                return;
            }
            upstream.setTcpNoDelay(true);
            Thread down = pump(upstream, client, false);
            pump(client, upstream, true);
            joinQuietly(down);
        } catch (Exception e) {
            Log.w(TAG, "relay conn failed: " + e);
        } finally {
            closeQuietly(client);
            closeQuietly(upstream);
        }
    }

    /** 先走手机 MCP 隧道；隧道没起来才退回直连（Wi-Fi 同网段 / 手机自己接了 VPN）。 */
    private static Socket openUpstream() {
        Socket s = socksConnect();
        if (s != null) {
            return s;
        }
        try {
            Socket d = new Socket();
            d.connect(new InetSocketAddress(targetHost, targetPort), 3000);
            Log.i(TAG, "relay: 直连兜底 " + targetHost + ":" + targetPort);
            return d;
        } catch (IOException e) {
            Log.w(TAG, "relay: 直连兜底失败 " + e);
            return null;
        }
    }

    /** SOCKS5 无认证握手（手机 MCP 的 tunnel_socks_port 只监听 127.0.0.1，无认证）。 */
    private static Socket socksConnect() {
        Socket s = null;
        try {
            s = new Socket();
            s.connect(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), socksPort), 3000);
            s.setSoTimeout(8000);
            InputStream in = s.getInputStream();
            OutputStream out = s.getOutputStream();
            out.write(new byte[]{5, 1, 0});
            out.flush();
            byte[] greet = new byte[2];
            readFully(in, greet);
            if (greet[0] != 5 || greet[1] != 0) {
                throw new IOException("socks greet " + greet[0] + "/" + greet[1]);
            }
            byte[] host = targetHost.getBytes("UTF-8");
            byte[] req = new byte[7 + host.length];
            req[0] = 5;
            req[1] = 1;
            req[2] = 0;
            req[3] = 3; // DOMAINNAME：让隧道那端解析
            req[4] = (byte) host.length;
            System.arraycopy(host, 0, req, 5, host.length);
            req[5 + host.length] = (byte) ((targetPort >> 8) & 0xFF);
            req[6 + host.length] = (byte) (targetPort & 0xFF);
            out.write(req);
            out.flush();
            byte[] rep = new byte[4];
            readFully(in, rep);
            if (rep[1] != 0) {
                throw new IOException("socks connect rep=" + rep[1]);
            }
            int skip = rep[3] == 1 ? 4 : rep[3] == 4 ? 16 : 0;
            if (rep[3] == 3) {
                int n = in.read();
                skip = n < 0 ? 0 : n;
            }
            readFully(in, new byte[skip + 2]);
            s.setSoTimeout(0);
            return s;
        } catch (Exception e) {
            Log.w(TAG, "relay: socks 127.0.0.1:" + socksPort + " 不可用（" + e + "）");
            closeQuietly(s);
            return null;
        }
    }

    private static void readFully(InputStream in, byte[] buf) throws IOException {
        int off = 0;
        while (off < buf.length) {
            int n = in.read(buf, off, buf.length - off);
            if (n < 0) {
                throw new IOException("socks eof");
            }
            off += n;
        }
    }

    private static Thread pump(final Socket from, final Socket to, boolean asThread) {
        Runnable r = new Runnable() {
            @Override
            public void run() {
                byte[] buf = new byte[8192];
                try {
                    InputStream in = from.getInputStream();
                    OutputStream out = to.getOutputStream();
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                        out.flush();
                    }
                } catch (Exception ignored) {
                } finally {
                    try { to.shutdownOutput(); } catch (Exception ignored) { }
                }
            }
        };
        if (!asThread) {
            Thread t = new Thread(r, "hub-relay-pump");
            t.setDaemon(true);
            t.start();
            return t;
        }
        r.run();
        return null;
    }

    private static void joinQuietly(Thread t) {
        if (t == null) {
            return;
        }
        try {
            t.join(30000);
        } catch (InterruptedException ignored) {
        }
    }

    private static void closeQuietly(java.io.Closeable c) {
        if (c != null) {
            try { c.close(); } catch (Exception ignored) { }
        }
    }
}
