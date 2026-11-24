// Clerk glue for the server-rendered dashboard (no inline scripts: strict CSP).
// Clerk Core 3: clerk-js v6 + the separate @clerk/ui bundle, whose
// constructor (window.__internal_ClerkUICtor) must be passed to Clerk.load().
// The server verifies the short-lived `__session` cookie that clerk-js keeps
// fresh; this file mounts sign-in/up, retries a page once the cookie is
// refreshed, and wires the sign-out button.
(function () {
  "use strict";
  var RETRY_KEY = "jg_auth_retry";

  function whenReady(fn) {
    var tries = 0;
    (function poll() {
      if (window.Clerk && window.__internal_ClerkUICtor) return fn();
      if (++tries > 150) return showMessage("Couldn't load sign-in. Check your connection and reload.");
      setTimeout(poll, 100);
    })();
  }

  function showMessage(text) {
    var el = document.getElementById("gate-msg");
    if (el) el.textContent = text;
  }

  function sameOrigin(url) {
    try { return new URL(url, location.href).origin === location.origin; } catch (e) { return false; }
  }

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function appearance() {
    var fg = cssVar("--fg", "#111418");
    var bg = cssVar("--bg", "#ffffff");
    var muted = cssVar("--muted", "#6b7280");
    var line = cssVar("--line", "#e7e8ea");
    var vars = {
      // Core 3 names, plus their older equivalents (unknown keys are ignored).
      colorPrimary: fg,
      colorPrimaryForeground: bg,
      colorTextOnPrimaryBackground: bg,
      colorBackground: bg,
      colorForeground: fg,
      colorText: fg,
      colorMutedForeground: muted,
      colorTextSecondary: muted,
      colorInput: bg,
      colorInputBackground: bg,
      colorInputForeground: fg,
      colorInputText: fg,
      colorBorder: line,
      colorNeutral: fg,
      colorDanger: cssVar("--warn", "#b42318"),
      fontFamily: getComputedStyle(document.body).fontFamily,
      fontSize: "14px",
      borderRadius: "8px",
    };
    var flat = { boxShadow: "none" };
    return {
      variables: vars,
      elements: {
        rootBox: { width: "100%" },
        cardBox: { boxShadow: "none", border: "1px solid " + line, width: "100%", maxWidth: "100%" },
        card: flat,
        footer: { background: "transparent" },
        formButtonPrimary: flat,
        socialButtonsBlockButton: flat,
      },
    };
  }

  window.addEventListener("load", function () {
    if (!document.querySelector("script[data-clerk-publishable-key]")) return;
    whenReady(async function () {
      try {
        await window.Clerk.load({
          ui: { ClerkUI: window.__internal_ClerkUICtor },
          signInUrl: "/login",
          signUpUrl: "/sign-up",
          signInFallbackRedirectUrl: "/board",
          signUpFallbackRedirectUrl: "/board",
          appearance: appearance(),
        });
      } catch (e) {
        showMessage("Sign-in failed to start. Check CLERK_PUBLISHABLE_KEY.");
        return;
      }
      var gate = document.getElementById("clerk-gate");
      if (gate && gate.dataset.state === "signed_out") {
        var retry = sameOrigin(gate.dataset.retry) ? gate.dataset.retry : "/board";
        if (window.Clerk.user) {
          // Signed in client-side; the server saw an expired/missing token.
          // clerk-js has refreshed the cookie now, so retry (at most twice).
          var n = Number(sessionStorage.getItem(RETRY_KEY) || 0);
          if (n < 2) {
            sessionStorage.setItem(RETRY_KEY, String(n + 1));
            location.replace(retry);
            return;
          }
          showMessage("You're signed in, but the server couldn't verify the session. " +
            "Check the server clock and CLERK_* settings, then reload.");
        } else {
          sessionStorage.removeItem(RETRY_KEY);
          var mount = document.getElementById("clerk-mount");
          mount.textContent = "";
          var target = new URL(retry, location.href).href;
          if (gate.dataset.mode === "sign-up") {
            window.Clerk.mountSignUp(mount, { forceRedirectUrl: target, signInUrl: "/login" });
          } else {
            window.Clerk.mountSignIn(mount, { forceRedirectUrl: target, signUpUrl: "/sign-up" });
          }
        }
      } else {
        sessionStorage.removeItem(RETRY_KEY);
      }
      var userSlot = document.getElementById("user-button");
      if (userSlot && window.Clerk.user && typeof window.Clerk.mountUserButton === "function") {
        userSlot.textContent = "";
        window.Clerk.mountUserButton(userSlot, {
          appearance: { elements: { rootBox: { width: "auto" }, avatarBox: { width: "30px", height: "30px" } } },
        });
      }
      var out = document.getElementById("signout");
      if (out) {
        out.addEventListener("click", async function () {
          out.disabled = true;
          await window.Clerk.signOut();
          location.replace("/login");
        });
      }
    });
  });
})();
