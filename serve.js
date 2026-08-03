const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 7584;
const BASE_DIR = __dirname;

const server = http.createServer((req, res) => {
    let filePath = req.url === '/' ? '/index.html' : req.url;
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

server.listen(PORT, '0.0.0.0', () => {
    console.log(`服务器已启动: http://localhost:${PORT}/`);
    console.log(`局域网访问: http://192.168.31.128:${PORT}/`);
});
