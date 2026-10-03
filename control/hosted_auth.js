/* Stage 1 only. Credentials and registration grants stay in memory, never storage. */
"use strict";
const byId = (id) => document.getElementById(id);
let email = "", challenge = "", registration = "";
const errors = {
  invalid_credentials: "Email or password is incorrect.",
  invalid_verification: "The code is incorrect, expired or already used. Request a new code.",
  invalid_registration: "Verification has expired. Please start registration again.",
  invalid_email: "Enter a valid email address.",
  invalid_password: "Use a password with 12–128 characters.",
  invalid_request: "Check the fields and try again.",
  auth_unavailable: "Email or authentication is temporarily unavailable. Please try again.",
};
function show(section) {
  for (const id of ["entry", "verify", "password", "signed-in"]) byId(id).hidden = id !== section;
}
async function call(path, body) {
  const response = await fetch(`/auth/${path}`, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = response.status === 204 ? {} : await response.json();
  if (!response.ok) {
    const retry = response.headers.get("Retry-After");
    throw new Error(retry ? `Please try again in ${retry} seconds.` :
      errors[payload.error] || "Unable to complete the request. Please try again.");
  }
  return payload;
}
async function action(work) {
  byId("status").textContent = "";
  for (const button of document.querySelectorAll("button")) button.disabled = true;
  try { await work(); }
  catch (error) { byId("status").textContent = error.message; }
  finally { for (const button of document.querySelectorAll("button")) button.disabled = false; }
}
function signedIn(payload) {
  email = ""; challenge = ""; registration = "";
  for (const form of document.querySelectorAll("form")) form.reset();
  byId("identity").textContent = payload.user.email;
  show("signed-in");
}
async function sendCode() {
  const result = await call("register", { email });
  challenge = result.challenge_id;
  registration = "";
  show("verify");
  byId("status").textContent = "If this address can register, a verification code has been sent.";
}
for (const id of ["login", "register", "verify", "password"]) {
  byId(id).addEventListener("submit", (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    action(async () => {
      if (id === "login") signedIn(await call("login", data));
      if (id === "register") { email = data.email; await sendCode(); }
      if (id === "verify") {
        const result = await call("verify", { challenge_id: challenge, code: data.code });
        registration = result.registration_token;
        byId("verify").reset();
        show("password");
      }
      if (id === "password") signedIn(await call("password", {
        registration_token: registration, password: data.password,
      }));
    });
  });
}
byId("resend").addEventListener("click", () => action(sendCode));
byId("back").addEventListener("click", () => {
  email = ""; challenge = ""; registration = "";
  show("entry"); byId("status").textContent = "";
});
byId("logout").addEventListener("click", () => action(async () => {
  await call("logout", {});
  byId("identity").textContent = "";
  show("entry");
}));
call("me").then(signedIn).catch(() => show("entry"));
