Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = "Выберите аудио или видео для Whisper"
$dialog.Filter = "Медиафайлы|*.avi;*.mkv;*.mp4;*.mov;*.webm;*.wmv;*.m4v;*.mp3;*.wav;*.flac;*.m4a;*.aac;*.ogg;*.opus;*.wma;*.aiff;*.ac3;*.ts;*.m2ts|Все файлы|*.*"
$dialog.Multiselect = $true
$dialog.CheckFileExists = $true

if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    @($dialog.FileNames) | ConvertTo-Json -Compress
} else {
    "[]"
}
