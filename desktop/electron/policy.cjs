const hosts = new Set([
  "mail.google.com",
  "app.slack.com",
  "discord.com",
  "outlook.live.com",
  "outlook.office.com",
  "outlook.office365.com",
]);
exports.isAllowedExternalUrl = (value) => {
  if (typeof value !== "string") return false;
  try {
    const u = new URL(value);
    return (
      u.protocol === "https:" &&
      !u.username &&
      !u.password &&
      !u.port &&
      hosts.has(u.hostname)
    );
  } catch {
    return false;
  }
};
