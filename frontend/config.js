// 로컬(docker-compose)은 프론트/게이트웨이가 포트가 달라서 localhost:8000이 필요하고,
// ECS/ALB 배포본은 같은 오리진이라 빈 문자열(상대경로)이어야 함 — 호스트명으로 자동 판별.
const isLocal = ["localhost", "127.0.0.1"].includes(window.location.hostname);
window.APP_CONFIG = {
  apiBase: isLocal ? "http://localhost:8000" : "",
};
