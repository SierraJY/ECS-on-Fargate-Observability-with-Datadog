FROM nginx:alpine

RUN apk add --no-cache curl

COPY index.html style.css app.js config.js /usr/share/nginx/html/

EXPOSE 80
