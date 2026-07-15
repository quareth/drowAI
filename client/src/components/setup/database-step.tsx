/**
 * Database credential step for the standalone setup wizard.
 */
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Database, Eye, EyeOff, RefreshCw, CheckCircle, AlertCircle } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { apiRequest } from '@/lib/queryClient';
import { SetupActions, SetupCallout, SetupStepHeader } from "@/components/setup/setup-ui";

interface DatabaseConfig {
  db_name: string;
  db_user: string;
  db_password: string;
}

interface DatabaseStepProps {
  config: DatabaseConfig;
  onUpdate: (data: Partial<DatabaseConfig>) => void;
  onNext: () => void;
  onPrevious: () => void;
}

export function DatabaseStep({ config, onUpdate, onNext, onPrevious }: DatabaseStepProps) {
  const [showPassword, setShowPassword] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const generatePasswordMutation = useMutation({
    mutationFn: async () => {
      return apiRequest('/api/setup/generate-secrets', { method: 'POST' });
    },
    onSuccess: (data: any) => {
      onUpdate({ db_password: data.db_password });
    }
  });

  const validateMutation = useMutation({
    mutationFn: async (data: DatabaseConfig) => {
      return apiRequest('/api/setup/validate-database', {
        method: 'POST',
        body: JSON.stringify(data)
      });
    },
    onSuccess: () => {
      setErrors({});
      onNext();
    },
    onError: (error: any) => {
      setErrors({ general: error.message || 'Database validation failed' });
    }
  });

  const handleInputChange = (field: keyof DatabaseConfig, value: string) => {
    onUpdate({ [field]: value });
    if (errors[field]) {
      setErrors(prev => ({ ...prev, [field]: '' }));
    }
  };

  const handleNext = () => {
    const newErrors: Record<string, string> = {};
    
    if (!config.db_name.trim()) {
      newErrors.db_name = 'Database name is required';
    }
    if (!config.db_user.trim()) {
      newErrors.db_user = 'Database username is required';
    }
    if (!config.db_password.trim()) {
      newErrors.db_password = 'Database password is required';
    }
    if (config.db_password.length < 8) {
      newErrors.db_password = 'Password must be at least 8 characters long';
    }

    if (Object.keys(newErrors).length > 0) {
      setErrors(newErrors);
      return;
    }

    validateMutation.mutate(config);
  };

  const generatePassword = () => {
    generatePasswordMutation.mutate();
  };

  return (
    <div className="space-y-6">
      <SetupStepHeader
        icon={Database}
        title="Database"
        description="Configure the PostgreSQL credentials used by the generated deployment config."
      />

      <SetupCallout>
        <div className="flex items-start gap-3">
          <CheckCircle className="mt-0.5 h-4 w-4 text-slate-400" />
          <div>
            <h3 className="font-medium text-slate-100">PostgreSQL is the control-plane database.</h3>
            <p className="mt-1 text-slate-400">
              The setup process writes these credentials into the generated deployment config.
            </p>
          </div>
        </div>
      </SetupCallout>

      <div className="space-y-4">
        <div>
          <Label htmlFor="db_name">Database Name</Label>
          <Input
            id="db_name"
            value={config.db_name}
            onChange={(e) => handleInputChange('db_name', e.target.value)}
            placeholder="drowai"
            className={errors.db_name ? 'border-red-500' : ''}
          />
          {errors.db_name && (
            <p className="text-sm text-red-600 mt-1">{errors.db_name}</p>
          )}
          <p className="text-xs text-slate-500 mt-1">
            The name of the database that will be created
          </p>
        </div>

        <div>
          <Label htmlFor="db_user">Database Username</Label>
          <Input
            id="db_user"
            value={config.db_user}
            onChange={(e) => handleInputChange('db_user', e.target.value)}
            placeholder="drowai_user"
            className={errors.db_user ? 'border-red-500' : ''}
          />
          {errors.db_user && (
            <p className="text-sm text-red-600 mt-1">{errors.db_user}</p>
          )}
          <p className="text-xs text-slate-500 mt-1">
            Username for database authentication
          </p>
        </div>

        <div>
          <Label htmlFor="db_password">Database Password</Label>
          <div className="relative">
            <Input
              id="db_password"
              type={showPassword ? 'text' : 'password'}
              value={config.db_password}
              onChange={(e) => handleInputChange('db_password', e.target.value)}
              placeholder="Enter a secure password"
              className={`pr-20 ${errors.db_password ? 'border-red-500' : ''}`}
            />
            <div className="absolute inset-y-0 right-0 flex items-center space-x-1 pr-3">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setShowPassword(!showPassword)}
                className="h-6 w-6 p-0"
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={generatePassword}
                disabled={generatePasswordMutation.isPending}
                className="h-6 w-6 p-0"
              >
                <RefreshCw className={`w-4 h-4 ${generatePasswordMutation.isPending ? 'animate-spin' : ''}`} />
              </Button>
            </div>
          </div>
          {errors.db_password && (
            <p className="text-sm text-red-600 mt-1">{errors.db_password}</p>
          )}
          <p className="text-xs text-slate-500 mt-1">
            Minimum 8 characters. Click refresh to generate a secure password.
          </p>
        </div>
      </div>

      {errors.general && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{errors.general}</AlertDescription>
        </Alert>
      )}

      <SetupCallout>
        <div className="flex items-start gap-3">
          <AlertCircle className="mt-0.5 h-4 w-4 text-slate-400" />
          <div>
            <h4 className="font-medium text-slate-100">Security note</h4>
            <p className="mt-1 text-slate-400">
              These credentials will be used by the deployment database service.
              Make sure to use a strong password and keep these credentials secure.
            </p>
          </div>
        </div>
      </SetupCallout>

      <SetupActions>
        <Button variant="outline" onClick={onPrevious} className="border-slate-700 bg-slate-950 text-slate-200 hover:bg-slate-800 hover:text-white">
          Previous
        </Button>
        
        <Button 
          onClick={handleNext}
          disabled={validateMutation.isPending}
          className="flex items-center space-x-2"
        >
          {validateMutation.isPending && <RefreshCw className="w-4 h-4 animate-spin" />}
          <span>{validateMutation.isPending ? 'Validating...' : 'Next'}</span>
        </Button>
      </SetupActions>
    </div>
  );
}
