import { useEffect, useState } from "react";
import { hostedRequest } from "./api";

export function AccountSettings() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {void hostedRequest("/auth/me").then(data => setEmail(data.user.email)).catch(e => setError(e.message));}, []);
  const logout = async () => {
    try {
      await hostedRequest("/auth/logout", {});
      window.location.assign(String(import.meta.env.VITE_API_BASE ?? "") + "/auth");
    } catch(e) {setError((e as Error).message);}
  };
  return <section className="settings-section"><h2>Your account</h2><p>{email}</p>{error && <p role="alert">{error}</p>}<button className="button" onClick={() => void logout()}>Sign out</button></section>;
}
