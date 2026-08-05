const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');

const PORT = 7584;
const BASE_DIR = __dirname;

const server = http.createServer((req, res) => {
    let urlPath = req.url.split('?')[0];
    let filePath = urlPath === '/' ? '/index.html' : urlPath;
    filePath = path.join(BASE_DIR, decodeURIComponent(filePath));

    fs.readFile(filePath, (err, data) => {
        if (err) {
            res.writeHead(404);
            res.end('Not Found');
            return;
        }
        const ext = path.extname(filePath);
        const types = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
        res.writeHead(200, { 'Content-Type': types[ext] || 'application/octet-stream' });
        res.end(data);
    });
});

function lanAddresses() {
    const list = [];
    const ifs = os.networkInterfaces();
    for (const name of Object.keys(ifs)) {
        for (const info of ifs[name]) {
            if (info.family === 'IPv4' && !info.internal) list.push(info.address);
        }
    }
    return list.filter(a => a.startsWith('192.') || a.startsWith('10.') || a.startsWith('172.'));
}

server.listen(PORT, '0.0.0.0', () => {
    console.log(`服务器已启动: http://localhost:${PORT}/`);
    for (const ip of lanAddresses()) {
        console.log(`局域网访问: http://${ip}:${PORT}/`);
    }
});
