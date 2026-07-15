import { useState } from "react";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Upload, X } from "lucide-react";
import { useToast } from "@/hooks/use-toast";

type FileDropUploadProps = {
  label?: string;
  accept: string[]; // e.g., ['.md', '.txt']
  maxBytes?: number; // default 10MB
  onLoadText: (text: string, file: File) => void;
  emptyHint?: string;
  inputId: string; // unique id for input/label association
  className?: string;
};

export function FileDropUpload({
  label = "File Upload",
  accept,
  maxBytes = 10 * 1024 * 1024,
  onLoadText,
  emptyHint = "Drag and drop a file or click to browse",
  inputId,
  className,
}: FileDropUploadProps) {
  const [dragActive, setDragActive] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const { toast } = useToast();

  const handleFile = (f: File) => {
    if (!f) return;
    if (f.size > maxBytes) {
      toast({
        title: "File too large",
        description: `Please select a file smaller than ${Math.round(maxBytes / (1024 * 1024))}MB.`,
        variant: "destructive",
      });
      return;
    }
    const ext = "." + (f.name.split(".").pop()?.toLowerCase() || "");
    if (!accept.includes(ext)) {
      toast({
        title: "Invalid file type",
        description: `Allowed: ${accept.join(", ")}`,
        variant: "destructive",
      });
      return;
    }
    setFile(f);
    const reader = new FileReader();
    reader.onload = (e) => {
      const text = (e.target?.result as string) || "";
      onLoadText(text, f);
      toast({ title: "File loaded", description: `${f.name} has been read.` });
    };
    reader.onerror = () => {
      toast({ title: "Upload failed", description: "Failed to read the file.", variant: "destructive" });
      setFile(null);
    };
    reader.readAsText(f);
  };

  const onDrag = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === "dragenter" || e.type === "dragover") setDragActive(true);
    else if (e.type === "dragleave") setDragActive(false);
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    const files = e.dataTransfer.files;
    if (files && files[0]) handleFile(files[0]);
  };

  return (
    <div className={className}>
      <Label className="text-gray-300 mb-2 block">{label}</Label>
      <div
        className={`border-2 border-dashed rounded-lg p-4 text-center transition-colors ${
          dragActive ? "border-blue-500 bg-blue-500/10" : "border-slate-600 hover:border-blue-500"
        }`}
        onDragEnter={onDrag}
        onDragLeave={onDrag}
        onDragOver={onDrag}
        onDrop={onDrop}
      >
        {file ? (
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <Upload className="w-5 h-5 text-green-400" />
              <span className="text-gray-300">{file.name}</span>
              <span className="text-gray-500 text-sm">({Math.round(file.size / 1024)} KB)</span>
            </div>
            <Button type="button" variant="ghost" size="sm" onClick={() => setFile(null)} className="text-red-400 hover:text-red-300">
              <X className="w-4 h-4" />
            </Button>
          </div>
        ) : (
          <>
            <Upload className="w-8 h-8 text-gray-400 mx-auto mb-2" />
            <div className="text-gray-400">{emptyHint}</div>
            <input
              type="file"
              accept={accept.join(",")}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) handleFile(f);
              }}
              className="hidden"
              id={inputId}
            />
            <Label htmlFor={inputId} className="mt-2 text-blue-400 hover:text-blue-300 cursor-pointer">
              Choose File
            </Label>
          </>
        )}
      </div>
    </div>
  );
}

