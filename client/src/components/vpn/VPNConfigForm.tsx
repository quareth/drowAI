import { useState } from 'react';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { useToast } from '@/hooks/use-toast';
import { FileDropUpload } from '@/components/ui/file-drop-upload';

export type VPNProvider = 'htb' | 'tryhackme' | 'custom';

export interface VPNConfig {
  provider: VPNProvider;
  config_data: string;
}

interface VPNConfigFormProps {
  onConfigChange: (config: VPNConfig) => void;
  initialConfig?: Partial<VPNConfig>;
}

export function VPNConfigForm({ onConfigChange, initialConfig }: VPNConfigFormProps) {
  const [provider, setProvider] = useState<VPNProvider>((initialConfig?.provider as VPNProvider) || 'custom');
  const [configData, setConfigData] = useState<string>(initialConfig?.config_data || '');
  const { toast } = useToast();

  const handleChange = (value: string) => {
    const p = value as VPNProvider;
    setProvider(p);
    onConfigChange({ provider: p, config_data: configData });
  };

  const onManualChange = (value: string) => {
    setConfigData(value);
    onConfigChange({ provider, config_data: value });
  };

  return (
    <div className="space-y-3">
      <div>
        <Label className="text-gray-300 mb-1 block">VPN Provider</Label>
        <Select value={provider} onValueChange={handleChange}>
          <SelectTrigger className="bg-slate-800 border-slate-600 text-white">
            <SelectValue placeholder="Select provider" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="htb">HackTheBox</SelectItem>
            <SelectItem value="tryhackme">TryHackMe</SelectItem>
            <SelectItem value="custom">Custom</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <FileDropUpload
        label="OVPN File Upload (optional)"
        accept={['.ovpn','.conf','.txt']}
        inputId="ovpn-upload"
        emptyHint="Drag and drop an .ovpn/.conf/.txt file or click to browse"
        onLoadText={(text, file) => {
          setConfigData(text);
          onConfigChange({ provider, config_data: text });
          toast({ title: 'OVPN loaded', description: `${file.name} has been loaded.` });
        }}
      />

      <div>
        <Label className="text-gray-300 mb-1 block">Manual Configuration</Label>
        <Textarea
          rows={6}
          placeholder={'client\nremote <host> 1194\ndev tun'}
          value={configData}
          onChange={(e) => onManualChange(e.target.value)}
          className="w-full bg-slate-800 border-slate-600 text-white font-mono text-sm"
        />
      </div>

      <div className="flex justify-end">
        <Button type="button" className="bg-blue-600 hover:bg-blue-700" onClick={() => onConfigChange({ provider, config_data: configData })}>
          Apply VPN Config
        </Button>
      </div>
    </div>
  );
}

