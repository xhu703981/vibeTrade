import type { NetworkInfo } from "../types";

/** Top-bar badge, shown only when the backend trades on Solana mainnet with real funds (amber: a key state). */
export function NetworkBadge(props: { network?: NetworkInfo | null }) {
  const n = props.network;
  if (!n?.real_funds) return null;
  const cap = n.max_trade_usd != null ? `, at most $${n.max_trade_usd} per trade` : "";
  return (
    <span className="net-badge" role="status" data-testid="mainnet-badge"
      title={`Solana mainnet: approved cards send real transactions${cap}`}>
      Mainnet <span className="net-sep" aria-hidden>·</span> Real funds
    </span>
  );
}
