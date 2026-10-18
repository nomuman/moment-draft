const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../moment_draft/static/app.js"), "utf8");
const apiSource = source.slice(source.indexOf("async function api("), source.indexOf("function path("));

function client(response) {
  const context = vm.createContext({ fetch: async () => response, Blob, session: { csrf: "test" } });
  vm.runInContext(apiSource, context);
  return context.api;
}

for (const status of [413, 502]) {
  test(`non-JSON HTTP ${status} retains status without reading the server body`, async () => {
    let cancelled = false;
    const api = client({
      ok: false,
      status,
      headers: new Headers({ "Content-Type": "text/html" }),
      json: () => { throw new Error("private server details"); },
      body: { cancel: async () => { cancelled = true; } },
    });
    await assert.rejects(api("/api/projects"), (error) => {
      assert.equal(error.status, status);
      assert.ok(error.message.includes("処理を完了できませんでした"));
      assert.ok(!error.message.includes("private server details"));
      return true;
    });
    assert.ok(cancelled);
  });
}

test("JSON API errors preserve the message and HTTP status", async () => {
  const api = client(new Response(JSON.stringify({ error: "画面を更新してください。" }), {
    status: 409, headers: { "Content-Type": "application/json" },
  }));
  await assert.rejects(api("/api/projects"), (error) => {
    assert.equal(error.message, "画面を更新してください。");
    assert.equal(error.status, 409);
    return true;
  });
});

test("malformed JSON errors retain the HTTP status", async () => {
  const api = client(new Response("private server details", {
    status: 503, headers: { "Content-Type": "application/json" },
  }));
  await assert.rejects(api("/api/projects"), (error) => {
    assert.equal(error.status, 503);
    assert.ok(!error.message.includes("private server details"));
    return true;
  });
});

test("successful JSON responses remain available to callers", async () => {
  const api = client(new Response(JSON.stringify({ offset: 8388608 }), {
    headers: { "Content-Type": "application/json; charset=utf-8" },
  }));
  assert.equal((await api("/api/projects")).offset, 8388608);
});
