// Reloj de Scanner-pump (Worker de Cloudflare «reloj-scanner-pump»).
// GitHub retrasa horas los workflows programados («schedule»), pero los que se lanzan a mano («workflow_dispatch»)
// arrancan en segundos. Este Worker lanza el workflow Predicción a las 00:25 UTC (19:25 hora Lima), justo después
// del cierre de la vela diaria, con un Cron Trigger de Cloudflare.
// Necesita el secreto GITHUB_TOKEN: una clave fine-grained de GitHub con acceso al repositorio y el permiso
// Actions: Read and write.
const REPO = "LuisP11127/Scanner-pump";
const WORKFLOW = "prediccion.yml";

async function lanzar(env) {
  const url = "https://api.github.com/repos/" + REPO + "/actions/workflows/" + WORKFLOW + "/dispatches";
  let ultimo = "";
  for (let intento = 1; intento <= 3; intento++) {
    const r = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.GITHUB_TOKEN,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "reloj-scanner-pump",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    if (r.status === 204) {
      console.log("Lanzado " + REPO + " " + WORKFLOW);
      return;
    }
    ultimo = r.status + " " + (await r.text()).slice(0, 300);
    if (r.status < 500 && r.status !== 429) break;  // 401, 403, 404: la clave no sirve y reintentar no ayuda
    await new Promise((listo) => setTimeout(listo, 20000));
  }
  throw new Error("No se pudo lanzar " + REPO + " " + WORKFLOW + ": " + ultimo);
}

export default {
  async scheduled(evento, env, ctx) {
    if (!env.GITHUB_TOKEN) throw new Error("Falta el secreto GITHUB_TOKEN");
    await lanzar(env);
  },
  // Página de estado: no lanza nada, solo dice si la clave está puesta.
  async fetch(peticion, env) {
    return Response.json({
      que_hace: "Lanza el workflow Predicción de " + REPO + " cada día a las 00:25 UTC (19:25 hora Lima).",
      clave_de_github: env.GITHUB_TOKEN ? "configurada" : "falta: añade el secreto GITHUB_TOKEN",
    });
  },
};
