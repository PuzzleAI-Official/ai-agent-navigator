import { useState, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Lock } from "lucide-react";

const HASH = "a1b2c3d4e5f6"; // obfuscation marker only

const PasswordGate = ({ children }: { children: React.ReactNode }) => {
  const [authenticated, setAuthenticated] = useState(() => {
    if (sessionStorage.getItem("site_auth") === "true") return true;
    const params = new URLSearchParams(window.location.search);
    const token = params.get("access");
    if (token === atob("MTJ2SUNIYmVWbHZqeTkzRA==")) {
      sessionStorage.setItem("site_auth", "true");
      // Strip token from URL
      params.delete("access");
      const clean = params.toString();
      const newUrl = window.location.pathname + (clean ? `?${clean}` : "") + window.location.hash;
      window.history.replaceState({}, "", newUrl);
      return true;
    }
    return false;
  });
  const [password, setPassword] = useState("");
  const [error, setError] = useState(false);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (password === atob("MTIzMTAyMTk=")) {
        sessionStorage.setItem("site_auth", "true");
        setAuthenticated(true);
        setError(false);
      } else {
        setError(true);
      }
    },
    [password]
  );

  if (authenticated) return <>{children}</>;

  return (
    <div className="min-h-screen bg-background flex items-center justify-center px-6">
      <form onSubmit={handleSubmit} className="w-full max-w-sm space-y-6 text-center">
        <div className="flex justify-center">
          <div className="w-14 h-14 border border-border flex items-center justify-center">
            <Lock className="w-6 h-6 text-foreground" />
          </div>
        </div>
        <div className="space-y-2">
          <h1 className="font-instrument-serif text-2xl text-foreground">Password Required</h1>
          <p className="text-sm text-muted-foreground">
            This site is currently in private access. Enter the password to continue.
          </p>
        </div>
        <div className="space-y-3">
          <Input
            type="password"
            placeholder="Enter password"
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
              setError(false);
            }}
            className={error ? "border-destructive" : ""}
            autoFocus
          />
          {error && (
            <p className="text-sm text-destructive">Incorrect password. Please try again.</p>
          )}
          <Button type="submit" className="w-full">
            Enter Site
          </Button>
        </div>
      </form>
    </div>
  );
};

export default PasswordGate;
