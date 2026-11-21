export default {
  async fetch(request, env) {
    const requestUrl = new URL(request.url);
    if (requestUrl.pathname.startsWith("/api/v1/") && env.CONTROL_CENTER_API_ORIGIN) {
      const apiUrl = new URL(request.url);
      apiUrl.protocol = new URL(env.CONTROL_CENTER_API_ORIGIN).protocol;
      apiUrl.host = new URL(env.CONTROL_CENTER_API_ORIGIN).host;
      return fetch(new Request(apiUrl, request));
    }
    const response = await env.ASSETS.fetch(request);
    const acceptsHtml = request.headers.get("accept")?.includes("text/html");

    if (response.status !== 404 || !acceptsHtml || !["GET", "HEAD"].includes(request.method)) {
      return response;
    }

    const indexUrl = new URL(request.url);
    indexUrl.pathname = "/index.html";
    indexUrl.search = "";
    return env.ASSETS.fetch(new Request(indexUrl, request));
  },
};
