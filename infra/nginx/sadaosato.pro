server {
    server_name sadaosato.pro www.sadaosato.pro;
    root /var/www/sadaosato;
    location = /reg { proxy_pass http://127.0.0.1:8081; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }
    location /r/ { proxy_pass http://127.0.0.1:8081; proxy_set_header Host $host; proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme; }
    location = /checkin { proxy_pass http://127.0.0.1:8081; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme; }
    location /admin/ {
        auth_basic "Sato Admin";
        auth_basic_user_file /etc/nginx/.sato_admin;
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location = /consent { try_files /consent.html =404; }
    location = /oferta { try_files /oferta.html =404; }
    location = /privacy { try_files /privacy.html =404; }
    location = /pretix { return 301 https://tickets.sadaosato.pro/control/; }
    location ^~ /pretix/ { return 301 https://tickets.sadaosato.pro/control/; }
    location = /hotels { try_files /hotels.html =404; }
    include /opt/sato/nginx-api.snippet;
    index index.html;

    gzip on;
    gzip_comp_level 5;
    gzip_min_length 1024;
    gzip_types text/css application/javascript application/json image/svg+xml text/plain application/xml;

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location ~* \.(?:css|js|jpg|jpeg|png|gif|ico|svg|woff2?)$ {
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    listen [::]:443 ssl ipv6only=on; # managed by Certbot
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/sadaosato.pro/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/sadaosato.pro/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot


}
server {
    if ($host = www.sadaosato.pro) {
        return 301 https://$host$request_uri;
    } # managed by Certbot


    if ($host = sadaosato.pro) {
        return 301 https://$host$request_uri;
    } # managed by Certbot


    listen 80;
    listen [::]:80;
    server_name sadaosato.pro www.sadaosato.pro;
    return 404; # managed by Certbot




}