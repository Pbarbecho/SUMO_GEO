# Frontend SUMO-GEO: nginx con los estáticos horneados y el proxy a `backend`
# (frontend/nginx.conf). Es la imagen que publica .github/workflows/publish-images.yml
# en ghcr.io/pbarbecho/sumo-geo-frontend para que los alumnos hagan `docker compose pull`.
FROM nginx:1.27-alpine
COPY . /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -f /usr/share/nginx/html/Dockerfile /usr/share/nginx/html/nginx.conf
EXPOSE 80
