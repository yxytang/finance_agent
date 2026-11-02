$ErrorActionPreference = 'Stop'
$documentPath = 'D:\recurrent\finance_agent\output\interview\个人财务分析系统_逐条技术讲解与面试准备.docx'
$renderDirectory = 'D:\recurrent\finance_agent\output\interview\_project_guide\render'
[System.IO.Directory]::CreateDirectory($renderDirectory) | Out-Null
$pdfPath = Join-Path $renderDirectory 'guide.pdf'
$wordApplication = $null
$guideDocument = $null
try {
    $wordApplication = New-Object -ComObject Word.Application
    $wordApplication.Visible = $false
    $wordApplication.DisplayAlerts = 0
    $guideDocument = $wordApplication.Documents.Open($documentPath, $false, $false)
    $guideDocument.Fields.Update() | Out-Null
    $guideDocument.Repaginate()
    $pageCount = $guideDocument.ComputeStatistics(2)
    $pageRecords = @()
    for ($pageNumber = 1; $pageNumber -le $pageCount; $pageNumber++) {
        $startRange = $guideDocument.GoTo(1, 1, $pageNumber)
        $pageStart = $startRange.Start
        if ($pageNumber -lt $pageCount) {
            $endRange = $guideDocument.GoTo(1, 1, ($pageNumber + 1))
            $pageEnd = $endRange.Start
        } else {
            $pageEnd = $guideDocument.Content.End
        }
        $pageText = $guideDocument.Range($pageStart, $pageEnd).Text
        $pageRecords += [pscustomobject]@{
            page = $pageNumber
            characters = $pageText.Length
            first = $pageText.Substring(0, [Math]::Min(85, $pageText.Length))
            last = $pageText.Substring([Math]::Max(0, $pageText.Length - 85))
        }
    }
    $guideDocument.Save()
    $guideDocument.ExportAsFixedFormat($pdfPath, 17)
    $pageRecords | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $renderDirectory 'pages.json') -Encoding utf8
    Write-Output "Rendered $pageCount pages with installed Word"
} finally {
    if ($null -ne $guideDocument) {
        $guideDocument.Close(0)
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($guideDocument)
    }
    if ($null -ne $wordApplication) {
        $wordApplication.Quit(0)
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($wordApplication)
    }
}
