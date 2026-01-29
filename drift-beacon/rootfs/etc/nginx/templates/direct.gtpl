{{ if .ssl }}
# HTTPS server on port 9000
server {
    listen {{ .port }} default_server ssl;

    include /etc/nginx/includes/server_params.conf;
    include /etc/nginx/includes/proxy_params.conf;
    include /etc/nginx/includes/ssl_params.conf;

    ssl_certificate {{ .certfile }};
    ssl_certificate_key {{ .keyfile }};

    location / {
        proxy_pass {{ .protocol }}://backend;
    }
}

# HTTP server on port 9001
server {
    listen {{ .http_port }};

    include /etc/nginx/includes/server_params.conf;
    include /etc/nginx/includes/proxy_params.conf;

    location / {
        proxy_pass {{ .protocol }}://backend;
    }
}
{{ else }}
# HTTP only server on port 9001
server {
    listen {{ .http_port }} default_server;

    include /etc/nginx/includes/server_params.conf;
    include /etc/nginx/includes/proxy_params.conf;

    location / {
        proxy_pass {{ .protocol }}://backend;
    }
}
{{ end }}