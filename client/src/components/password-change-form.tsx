/**
 * Password change form for authenticated account security management.
 */
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { useToast } from "@/hooks/use-toast";
import { apiRequest } from "@/lib/queryClient";
import { Lock, Eye, EyeOff, CheckCircle, AlertTriangle } from "lucide-react";

const passwordChangeSchema = z.object({
  old_password: z.string().min(1, "Current password is required"),
  new_password: z.string().min(6, "New password must be at least 6 characters long"),
  confirm_password: z.string().min(1, "Please confirm your new password")
}).refine((data) => data.new_password === data.confirm_password, {
  message: "Passwords don't match",
  path: ["confirm_password"]
});

type PasswordChangeData = z.infer<typeof passwordChangeSchema>;

export function PasswordChangeForm() {
  const [showCurrentPassword, setShowCurrentPassword] = useState(false);
  const [showNewPassword, setShowNewPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const { toast } = useToast();

  const form = useForm<PasswordChangeData>({
    resolver: zodResolver(passwordChangeSchema),
    defaultValues: {
      old_password: "",
      new_password: "",
      confirm_password: ""
    }
  });

  const passwordChangeMutation = useMutation({
    mutationFn: async (data: { old_password: string; new_password: string }) => {
      const response = await apiRequest("POST", "/api/auth/change-password", data);
      return response.json();
    },
    onSuccess: () => {
      toast({
        title: "Password Changed",
        description: "Your password has been updated successfully.",
        variant: "default"
      });
      form.reset();
    },
    onError: (error: Error) => {
      toast({
        title: "Password Change Failed",
        description: error.message.includes("401") 
          ? "Current password is incorrect" 
          : "Failed to change password. Please try again.",
        variant: "destructive"
      });
    }
  });

  const onSubmit = (data: PasswordChangeData) => {
    passwordChangeMutation.mutate({
      old_password: data.old_password,
      new_password: data.new_password
    });
  };

  const getPasswordStrength = (password: string) => {
    let strength = 0;
    if (password.length >= 8) strength++;
    if (/[A-Z]/.test(password)) strength++;
    if (/[a-z]/.test(password)) strength++;
    if (/[0-9]/.test(password)) strength++;
    if (/[^A-Za-z0-9]/.test(password)) strength++;
    return strength;
  };

  const getStrengthColor = (strength: number) => {
    switch (strength) {
      case 0:
      case 1:
        return "bg-red-500";
      case 2:
        return "bg-yellow-500";
      case 3:
        return "bg-blue-500";
      case 4:
      case 5:
        return "bg-green-500";
      default:
        return "bg-gray-500";
    }
  };

  const getStrengthText = (strength: number) => {
    switch (strength) {
      case 0:
      case 1:
        return "Weak";
      case 2:
        return "Fair";
      case 3:
        return "Good";
      case 4:
      case 5:
        return "Strong";
      default:
        return "";
    }
  };

  const newPassword = form.watch("new_password");
  const passwordStrength = getPasswordStrength(newPassword);

  return (
    <Card className="bg-slate-900 border-slate-700">
      <CardHeader>
        <CardTitle className="text-white flex items-center">
          <Lock className="w-5 h-5 mr-2 text-blue-300" />
          Password Management
        </CardTitle>
        <CardDescription className="text-gray-400">
          Change the password used to authenticate this account.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,1fr)_360px]">
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-5">
              <FormField
                control={form.control}
                name="old_password"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="text-gray-300">Current Password</FormLabel>
                    <FormControl>
                      <div className="relative">
                        <Input
                          {...field}
                          type={showCurrentPassword ? "text" : "password"}
                          placeholder="Enter your current password"
                          className="bg-slate-800 border-slate-600 text-white pr-10"
                        />
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="absolute right-0 top-0 h-full px-3 py-2 hover:bg-transparent"
                          onClick={() => setShowCurrentPassword(!showCurrentPassword)}
                        >
                          {showCurrentPassword ? (
                            <EyeOff className="h-4 w-4 text-gray-400" />
                          ) : (
                            <Eye className="h-4 w-4 text-gray-400" />
                          )}
                        </Button>
                      </div>
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="new_password"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="text-gray-300">New Password</FormLabel>
                    <FormControl>
                      <div className="relative">
                        <Input
                          {...field}
                          type={showNewPassword ? "text" : "password"}
                          placeholder="Enter your new password"
                          className="bg-slate-800 border-slate-600 text-white pr-10"
                        />
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="absolute right-0 top-0 h-full px-3 py-2 hover:bg-transparent"
                          onClick={() => setShowNewPassword(!showNewPassword)}
                        >
                          {showNewPassword ? (
                            <EyeOff className="h-4 w-4 text-gray-400" />
                          ) : (
                            <Eye className="h-4 w-4 text-gray-400" />
                          )}
                        </Button>
                      </div>
                    </FormControl>
                    {newPassword && (
                      <div className="mt-2">
                        <div className="flex items-center justify-between text-xs mb-1">
                          <span className="text-gray-400">Password Strength</span>
                          <span className={`font-medium ${
                            passwordStrength >= 4 ? 'text-green-400' : 
                            passwordStrength >= 3 ? 'text-blue-400' : 
                            passwordStrength >= 2 ? 'text-yellow-400' : 'text-red-400'
                          }`}>
                            {getStrengthText(passwordStrength)}
                          </span>
                        </div>
                        <div className="h-2 w-full rounded-full bg-slate-700">
                          <div 
                            className={`h-2 rounded-full transition-all duration-300 ${getStrengthColor(passwordStrength)}`}
                            style={{ width: `${(passwordStrength / 5) * 100}%` }}
                          />
                        </div>
                      </div>
                    )}
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="confirm_password"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel className="text-gray-300">Confirm New Password</FormLabel>
                    <FormControl>
                      <div className="relative">
                        <Input
                          {...field}
                          type={showConfirmPassword ? "text" : "password"}
                          placeholder="Confirm your new password"
                          className="bg-slate-800 border-slate-600 text-white pr-10"
                        />
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className="absolute right-0 top-0 h-full px-3 py-2 hover:bg-transparent"
                          onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                        >
                          {showConfirmPassword ? (
                            <EyeOff className="h-4 w-4 text-gray-400" />
                          ) : (
                            <Eye className="h-4 w-4 text-gray-400" />
                          )}
                        </Button>
                      </div>
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <Button
                type="submit"
                disabled={passwordChangeMutation.isPending}
                className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50"
              >
                {passwordChangeMutation.isPending ? (
                  "Saving Password..."
                ) : (
                  <>
                    <CheckCircle className="w-4 h-4 mr-2" />
                    Save Password
                  </>
                )}
              </Button>
            </form>
          </Form>

          <aside className="space-y-4">
            <div className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
              <div className="flex items-center gap-2 text-sm font-medium text-slate-100">
                <AlertTriangle className="h-4 w-4 text-yellow-300" />
                Password Requirements
              </div>
              <ul className="mt-3 space-y-2 text-sm text-slate-400">
                <li>At least 6 characters long.</li>
                <li>Use mixed case letters when possible.</li>
                <li>Add numbers or symbols for stronger protection.</li>
              </ul>
            </div>

            <div className="rounded-lg border border-slate-700 bg-slate-800/60 p-4">
              <h3 className="text-sm font-medium text-slate-100">Update Policy</h3>
              <p className="mt-2 text-sm leading-6 text-slate-400">
                The current password is required before this account credential can be changed.
              </p>
            </div>
          </aside>
        </div>
      </CardContent>
    </Card>
  );
}
