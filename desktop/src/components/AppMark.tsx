import {
  EnvelopeSimpleIcon,
  SlackLogoIcon,
  DiscordLogoIcon,
  StackIcon,
} from "@phosphor-icons/react";
import type { AppId } from "../types";

export default function AppMark({
  app,
  size = 20,
  className = "",
}: {
  app: AppId | "all";
  size?: number;
  className?: string;
}) {
  return (
    <span className={`app-mark ${className}`} data-app={app} aria-hidden="true">
      {app === "all" ? (
        <StackIcon size={size} />
      ) : app === "slack" ? (
        <SlackLogoIcon size={size} />
      ) : app === "discord" ? (
        <DiscordLogoIcon size={size} />
      ) : app === "gmail" ? (
        <EnvelopeSimpleIcon size={size} />
      ) : (
        <span className="outlook-mark">O</span>
      )}
    </span>
  );
}
