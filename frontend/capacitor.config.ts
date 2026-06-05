import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "com.codingdashboard.app",
  appName: "Coding Dashboard",
  webDir: "dist",
  server: {
    androidScheme: "https",
    // If your backend has no TLS (http://), uncomment to allow cleartext.
    // Strongly prefer real HTTPS instead.
    // cleartext: true,
  },
};

export default config;
