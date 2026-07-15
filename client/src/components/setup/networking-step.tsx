/**
 * Reserved networking settings step for future setup wizard configuration.
 */
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Network } from "lucide-react";

import type { SetupNetworkConfig } from "@/components/setup/setup-types";

interface NetworkingStepProps {
  config: SetupNetworkConfig;
  onUpdate: (data: Partial<SetupNetworkConfig>) => void;
  onNext: () => void;
  onPrevious: () => void;
}

export function NetworkingStep({ config, onUpdate, onNext, onPrevious }: NetworkingStepProps) {
  return (
    <div className="space-y-6">
      <div className="text-center">
        <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-cyan-100 dark:bg-cyan-900">
          <Network className="h-8 w-8 text-cyan-700 dark:text-cyan-300" />
        </div>
        <h2 className="mb-2 text-2xl font-bold text-slate-900 dark:text-white">Networking</h2>
        <p className="text-slate-600 dark:text-slate-300">
          Placeholder settings stored for future Runner Site and runtime network readiness.
        </p>
        <Badge variant="secondary" className="mt-3">
          Coming soon
        </Badge>
      </div>

      <div className="space-y-4">
        <div>
          <Label htmlFor="management_ip">Management IP</Label>
          <Input
            id="management_ip"
            value={config.management_ip || ""}
            onChange={(event) => onUpdate({ management_ip: event.target.value })}
            placeholder="10.0.0.1"
          />
        </div>
        <div>
          <Label htmlFor="gateway">Gateway</Label>
          <Input
            id="gateway"
            value={config.gateway || ""}
            onChange={(event) => onUpdate({ gateway: event.target.value })}
            placeholder="10.0.0.254"
          />
        </div>
        <div>
          <Label htmlFor="dns_servers">DNS Servers</Label>
          <Input
            id="dns_servers"
            value={config.dns_servers || ""}
            onChange={(event) => onUpdate({ dns_servers: event.target.value })}
            placeholder="1.1.1.1, 8.8.8.8"
          />
        </div>
        <div>
          <Label htmlFor="domain">Domain</Label>
          <Input
            id="domain"
            value={config.domain || ""}
            onChange={(event) => onUpdate({ domain: event.target.value })}
            placeholder="drowai.local"
          />
        </div>
        <div>
          <Label htmlFor="kali_docker_network">Kali Runtime Network</Label>
          <Input
            id="kali_docker_network"
            value={config.kali_docker_network || ""}
            onChange={(event) => onUpdate({ kali_docker_network: event.target.value })}
            placeholder="drowai-runtime-net"
          />
        </div>
      </div>

      <div className="flex justify-between">
        <Button variant="outline" onClick={onPrevious}>
          Previous
        </Button>
        <Button onClick={onNext}>Next</Button>
      </div>
    </div>
  );
}
