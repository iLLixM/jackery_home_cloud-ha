#Requires -Version 5.1

<#
.SYNOPSIS
    Read-only diagnostic client for the unofficial Jackery Home Cloud REST API.

.DESCRIPTION
    Validates a Jackery Home Cloud login and optionally retrieves account,
    system, device, monitor, and MQTT configuration data. The script uses only
    PowerShell and .NET APIs and does not require additional modules.

    If no follow-up action is selected, the script runs the default smoke test:
    app user, systems, devices for the first system, and MQTT configuration.

.PARAMETER Account
    Jackery account email or login name.

.PARAMETER PlainPassword
    Plain-text password used with the default, non-encrypted login flow.

.PARAMETER EncryptedPassword
    Password that has already been encrypted for the Jackery login API. The
    script does not encrypt passwords itself. Use together with -Encrypted.

.PARAMETER Password
    Generic password input, interpreted according to -Encrypted. Prefer the
    environment variable JACKERY_PASSWORD or the interactive prompt to avoid
    exposing a password in the command history.

.PARAMETER Encrypted
    Set the login payload field "encrypted" to true.

.PARAMETER Insecure
    Disable HTTPS certificate validation. Use only for controlled diagnostics.

.PARAMETER Output
    Select pretty-printed or compact JSON output.

.PARAMETER SavePath
    Save all collected results to a UTF-8 JSON file.

.EXAMPLE
    $env:JACKERY_PASSWORD = 'your-password'
    .\jackery-api-test.ps1 -account 'you@example.com' -login-only

.EXAMPLE
    .\jackery-api-test.ps1 -account 'you@example.com' -list-devices -all-systems

.EXAMPLE
    .\jackery-api-test.ps1 -account 'you@example.com' -show-monitor -system-id '2000000000000000001' -save '.\result.json'

.NOTES
    This client is based on reverse-engineered API behavior and may stop working
    if Jackery changes its backend or expected application headers.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string] $Account,

    [string] $Password,
    [Alias('plain-password')]
    [string] $PlainPassword,

    [Alias('encrypted-password')]
    [string] $EncryptedPassword,

    [switch] $Encrypted,

    [ValidateNotNullOrEmpty()]
    [Alias('phone-uid')]
    [string] $PhoneUid = 'sample-id-123',

    [ValidateNotNullOrEmpty()]
    [Alias('base-url')]
    [uri] $BaseUrl = 'https://prodeu-energymanagement-api.hello-tech.com:8000/geneverse-iot-gateway',

    [ValidateRange(1, 3600)]
    [Alias('timeout')]
    [int] $TimeoutSeconds = 20,

    [switch] $Insecure,

    [Alias('login-only')]
    [switch] $LoginOnly,

    [Alias('show-login-status')]
    [switch] $ShowLoginStatus,

    [Alias('show-app-user')]
    [switch] $ShowAppUser,

    [Alias('list-systems')]
    [switch] $ListSystems,

    [Alias('system-detail')]
    [switch] $SystemDetail,

    [Alias('list-devices')]
    [switch] $ListDevices,

    [Alias('show-monitor')]
    [switch] $ShowMonitor,

    [Alias('show-diy-devices')]
    [switch] $ShowDiyDevices,

    [Alias('show-mqtt')]
    [switch] $ShowMqtt,

    [Alias('device-detail')]
    [switch] $DeviceDetail,

    [Alias('ct-detail')]
    [switch] $CtDetail,

    [Alias('system-id')]
    [string] $SystemId,

    [Alias('all-systems')]
    [switch] $AllSystems,

    [Alias('device-no')]
    [string] $DeviceNo,

    [ValidateSet('Pretty', 'Json')]
    [string] $Output = 'Pretty',

    [Alias('save')]
    [string] $SavePath
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = 'Stop'

# System.Net.Http is part of .NET Framework on Windows 11, but Windows
# PowerShell 5.1 does not always load the assembly automatically.
Add-Type -AssemblyName System.Net.Http

$script:AccessToken = $null
$script:RefreshToken = $null
$script:TokenPrefix = 'Bearer'
$script:UserInfo = $null
$script:HttpClient = $null

$script:ClientHeaders = [ordered]@{
    'user-agent'      = 'Dart/3.11 (dart:io)'
    'accept-language' = 'en-US'
    'model'           = 'Phone'
    'accept-encoding' = 'gzip'
    'x-app-name'      = 'Custom-Phone'
    'x-app-version'   = 'home_android_v2.10.22'
    'sdkint'          = '34'
    'id'              = 'UP1A.231105.003.A1'
    'userend'         = 'HOME'
}

function ConvertFrom-SecureStringToPlainText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [Security.SecureString] $SecureValue
    )

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Resolve-JackeryPassword {
    [CmdletBinding()]
    param()

    if ($Encrypted) {
        if (-not [string]::IsNullOrWhiteSpace($EncryptedPassword)) {
            return $EncryptedPassword
        }
        if (-not [string]::IsNullOrWhiteSpace($Password)) {
            return $Password
        }
        throw [ArgumentException]::new(
            'Encrypted login requires -EncryptedPassword or -Password. The script cannot create the encrypted value.'
        )
    }

    if (-not [string]::IsNullOrWhiteSpace($PlainPassword)) {
        return $PlainPassword
    }
    if (-not [string]::IsNullOrWhiteSpace($Password)) {
        return $Password
    }
    if (-not [string]::IsNullOrWhiteSpace($env:JACKERY_PASSWORD)) {
        return $env:JACKERY_PASSWORD
    }

    if ([Environment]::UserInteractive) {
        $securePassword = Read-Host -Prompt 'Jackery password' -AsSecureString
        $plainText = ConvertFrom-SecureStringToPlainText -SecureValue $securePassword
        if (-not [string]::IsNullOrWhiteSpace($plainText)) {
            return $plainText
        }
    }

    throw [ArgumentException]::new(
        'Plain login requires -PlainPassword, -Password, JACKERY_PASSWORD, or a non-empty interactive password.'
    )
}

function New-JackeryHttpClient {
    [CmdletBinding()]
    param()

    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AutomaticDecompression =
        [Net.DecompressionMethods]::GZip -bor [Net.DecompressionMethods]::Deflate

    if ($Insecure) {
        # Use a typed .NET callback. A PowerShell script block may be invoked
        # by HttpClient on a worker thread without an available runspace in
        # Windows PowerShell 5.1. The callback remains local to this handler.
        if (-not ('JackeryHomeCertificateValidation' -as [type])) {
            Add-Type -ReferencedAssemblies 'System.Net.Http' -TypeDefinition @'
using System;
using System.Net.Http;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;

public static class JackeryHomeCertificateValidation
{
    public static Func<HttpRequestMessage, X509Certificate2, X509Chain, SslPolicyErrors, bool> Callback
    {
        get { return AcceptAny; }
    }

    private static bool AcceptAny(
        HttpRequestMessage request,
        X509Certificate2 certificate,
        X509Chain chain,
        SslPolicyErrors errors)
    {
        return true;
    }
}
'@
        }
        $handler.ServerCertificateCustomValidationCallback =
            [JackeryHomeCertificateValidation]::Callback
    }

    $client = [Net.Http.HttpClient]::new($handler, $true)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    return $client
}

function Test-JackeryProperty {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [object] $InputObject,

        [Parameter(Mandatory)]
        [string] $Name
    )

    if ($null -eq $InputObject) {
        return $false
    }
    if ($InputObject -is [Collections.IDictionary]) {
        return $InputObject.Contains($Name)
    }
    return $null -ne $InputObject.PSObject.Properties[$Name]
}

function Get-JackeryProperty {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [object] $InputObject,

        [Parameter(Mandatory)]
        [string] $Name
    )

    if ($null -eq $InputObject) {
        return $null
    }
    if ($InputObject -is [Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) {
            return $InputObject[$Name]
        }
        return $null
    }

    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -ne $property) {
        return $property.Value
    }
    return $null
}

function Set-JackeryProperty {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object] $InputObject,

        [Parameter(Mandatory)]
        [string] $Name,

        [AllowNull()]
        [object] $Value
    )

    if ($InputObject -is [Collections.IDictionary]) {
        $InputObject[$Name] = $Value
        return
    }
    $InputObject | Add-Member -MemberType NoteProperty -Name $Name -Value $Value -Force
}

function Get-JackeryHeaders {
    [CmdletBinding()]
    param(
        [switch] $Authenticated
    )

    $headers = [ordered]@{}
    foreach ($entry in $script:ClientHeaders.GetEnumerator()) {
        $headers[$entry.Key] = $entry.Value
    }

    if ($Authenticated) {
        if ([string]::IsNullOrWhiteSpace($script:AccessToken)) {
            throw [InvalidOperationException]::new(
                'No access token is available. Run the login request first.'
            )
        }
        $headers['authorization'] = "$($script:TokenPrefix) $($script:AccessToken)"
    }

    return $headers
}

function Invoke-JackeryRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet('GET', 'POST')]
        [string] $Method,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string] $Path,

        [AllowNull()]
        [object] $JsonBody,

        [switch] $Authenticated
    )

    $requestUri = '{0}{1}' -f $BaseUrl.AbsoluteUri.TrimEnd('/'), $Path
    $request = [Net.Http.HttpRequestMessage]::new(
        [Net.Http.HttpMethod]::new($Method),
        $requestUri
    )

    try {
        foreach ($entry in (Get-JackeryHeaders -Authenticated:$Authenticated).GetEnumerator()) {
            if (-not $request.Headers.TryAddWithoutValidation($entry.Key, [string] $entry.Value)) {
                throw [InvalidOperationException]::new(
                    "Could not add HTTP header '$($entry.Key)'."
                )
            }
        }

        if ($PSBoundParameters.ContainsKey('JsonBody') -and $null -ne $JsonBody) {
            $bodyText = $JsonBody | ConvertTo-Json -Depth 100 -Compress
            $request.Content = [Net.Http.StringContent]::new(
                $bodyText,
                [Text.Encoding]::UTF8,
                'application/json'
            )
        }

        try {
            $response = $script:HttpClient.SendAsync($request).GetAwaiter().GetResult()
        }
        catch {
            throw [InvalidOperationException]::new(
                "Request failed for ${Path}: $($_.Exception.Message)",
                $_.Exception
            )
        }

        try {
            $responseText = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()

            if (-not $response.IsSuccessStatusCode) {
                $excerpt = if ($responseText.Length -gt 500) {
                    $responseText.Substring(0, 500)
                }
                else {
                    $responseText
                }
                throw [InvalidOperationException]::new(
                    "HTTP error for ${Path}: $([int] $response.StatusCode) $excerpt"
                )
            }

            try {
                # Windows PowerShell 5.1 has neither -AsHashtable nor -Depth
                # on ConvertFrom-Json. Access to the resulting PSCustomObject
                # is normalized by the property helper functions above.
                $data = $responseText | ConvertFrom-Json
            }
            catch {
                $excerpt = if ($responseText.Length -gt 500) {
                    $responseText.Substring(0, 500)
                }
                else {
                    $responseText
                }
                throw [InvalidOperationException]::new(
                    "Response is not valid JSON for ${Path}: $excerpt",
                    $_.Exception
                )
            }

            if (
                -not (Test-JackeryProperty -InputObject $data -Name 'success') -or
                -not [bool] (Get-JackeryProperty -InputObject $data -Name 'success')
            ) {
                $apiCode = Get-JackeryProperty -InputObject $data -Name 'code'
                $apiMessage = Get-JackeryProperty -InputObject $data -Name 'msg'
                throw [InvalidOperationException]::new(
                    "API error for ${Path}: code=$apiCode msg=$apiMessage"
                )
            }

            return $data
        }
        finally {
            if ($null -ne $response) {
                $response.Dispose()
            }
        }
    }
    finally {
        $request.Dispose()
    }
}

function Connect-JackeryHome {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $ResolvedPassword
    )

    $payload = [ordered]@{
        encrypted  = [bool] $Encrypted
        userEnd    = 'HOME'
        userType   = '2'
        account    = $Account
        password   = $ResolvedPassword
        phoneUid   = $PhoneUid
        loginType  = 1
        rememberMe = $false
        clientType = 'APP'
    }

    $data = Invoke-JackeryRequest `
        -Method POST `
        -Path '/geneverse-iot-home/v1/home/auth/login' `
        -JsonBody $payload

    $result = Get-JackeryProperty -InputObject $data -Name 'result'
    if ($null -eq $result) {
        throw [InvalidOperationException]::new('Login succeeded but the result object is missing.')
    }

    $returnedTokenPrefix = Get-JackeryProperty -InputObject $result -Name 'tokenPrefix'
    $script:TokenPrefix = if ($returnedTokenPrefix) { [string] $returnedTokenPrefix } else { 'Bearer' }
    $script:AccessToken = [string] (Get-JackeryProperty -InputObject $result -Name 'accessToken')
    $script:RefreshToken = [string] (Get-JackeryProperty -InputObject $result -Name 'refreshToken')
    $script:UserInfo = Get-JackeryProperty -InputObject $result -Name 'userInfo'

    if ([string]::IsNullOrWhiteSpace($script:AccessToken)) {
        throw [InvalidOperationException]::new('Login succeeded but accessToken is missing.')
    }

    return $data
}

function Get-JackeryLoginStatus {
    Invoke-JackeryRequest -Method GET -Path '/geneverse-iot-home/v1/home/auth/loginStatus' -Authenticated
}

function Get-JackeryAppUser {
    Invoke-JackeryRequest -Method GET -Path '/geneverse-iot-home/v1/appUser/getOne' -Authenticated
}

function Get-JackerySystems {
    $data = Invoke-JackeryRequest -Method GET -Path '/geneverse-iot-home/v1/system/listByUserV2' -Authenticated
    $result = Get-JackeryProperty -InputObject $data -Name 'result'
    if ($null -eq $result) { return @() }
    return @($result)
}

function Get-JackerySystemDetail {
    param([Parameter(Mandatory)][string] $TargetSystemId)
    $escapedId = [Uri]::EscapeDataString($TargetSystemId)
    Invoke-JackeryRequest -Method GET -Path "/geneverse-iot-home/v1/system/$escapedId" -Authenticated
}

function Get-JackeryMonitor {
    param([Parameter(Mandatory)][string] $TargetSystemId)
    Invoke-JackeryRequest `
        -Method POST `
        -Path '/geneverse-iot-home/v1/app/monitor/' `
        -JsonBody @{ systemId = $TargetSystemId } `
        -Authenticated
}

function Get-JackeryDevices {
    param([Parameter(Mandatory)][string] $TargetSystemId)
    $escapedId = [Uri]::EscapeDataString($TargetSystemId)
    $data = Invoke-JackeryRequest `
        -Method GET `
        -Path "/geneverse-iot-home/v2/home/device/bySystemId/$escapedId" `
        -Authenticated
    $result = Get-JackeryProperty -InputObject $data -Name 'result'
    if ($null -eq $result) { return @() }
    return @($result)
}

function Get-JackeryDiyDevices {
    param([Parameter(Mandatory)][string] $TargetSystemId)
    $escapedId = [Uri]::EscapeDataString($TargetSystemId)
    Invoke-JackeryRequest `
        -Method GET `
        -Path "/geneverse-iot-home/v1/home/device/diyEpcDeviceList?systemId=$escapedId" `
        -Authenticated
}

function Get-JackeryDeviceDetail {
    param([Parameter(Mandatory)][string] $TargetDeviceNo)
    $escapedDeviceNo = [Uri]::EscapeDataString($TargetDeviceNo)
    Invoke-JackeryRequest `
        -Method GET `
        -Path "/geneverse-iot-home/v1/home/device/detail?deviceNo=$escapedDeviceNo" `
        -Authenticated
}

function Get-JackeryCtDetail {
    param([Parameter(Mandatory)][string] $TargetDeviceNo)
    $escapedDeviceNo = [Uri]::EscapeDataString($TargetDeviceNo)
    Invoke-JackeryRequest `
        -Method GET `
        -Path "/geneverse-iot-home/v1/home/device/ct/detail?deviceNo=$escapedDeviceNo" `
        -Authenticated
}

function Get-JackeryMqttConfig {
    $endpoints = @(
        [ordered]@{
            Path                = '/geneverse-iot-home/v2/idc/config/mqttServer'
            SourceEndpoint      = 'v2'
            PasswordIsPlaintext = $true
        },
        [ordered]@{
            Path                = '/geneverse-iot-home/v1/idc/config/mqttServer'
            SourceEndpoint      = 'v1'
            PasswordIsPlaintext = $false
        }
    )

    $lastError = $null
    foreach ($endpoint in $endpoints) {
        try {
            $data = Invoke-JackeryRequest -Method GET -Path $endpoint.Path -Authenticated
            $result = Get-JackeryProperty -InputObject $data -Name 'result'
            if (
                $null -ne $result -and
                (Get-JackeryProperty -InputObject $result -Name 'mqttServer') -and
                (Get-JackeryProperty -InputObject $result -Name 'mqttUserName') -and
                (Get-JackeryProperty -InputObject $result -Name 'mqttPassword')
            ) {
                Set-JackeryProperty -InputObject $result -Name '_source_endpoint' -Value $endpoint.SourceEndpoint
                Set-JackeryProperty -InputObject $result -Name '_password_is_plaintext' -Value $endpoint.PasswordIsPlaintext
                return $result
            }
        }
        catch {
            $lastError = $_
        }
    }

    if ($null -ne $lastError) {
        throw $lastError
    }
    throw [InvalidOperationException]::new(
        'MQTT response did not contain complete credentials.'
    )
}

function Get-MaskedAccount {
    param([Parameter(Mandatory)][string] $Value)

    if (-not $Value.Contains('@')) {
        if ($Value.Length -gt 3) { return "$($Value.Substring(0, 3))***" }
        return '***'
    }

    $parts = $Value.Split('@', 2)
    $local = $parts[0]
    $domain = $parts[1]
    if ($local.Length -gt 2) {
        $maskedLocal = "$($local.Substring(0, 2))***"
    }
    elseif ($local.Length -gt 0) {
        $maskedLocal = "$($local.Substring(0, 1))*"
    }
    else {
        $maskedLocal = '*'
    }
    return "$maskedLocal@$domain"
}

function ConvertTo-JackeryJson {
    param(
        [Parameter(Mandatory)]
        [AllowNull()]
        [object] $Data,

        [switch] $Compact
    )

    return $Data | ConvertTo-Json -Depth 100 -Compress:$Compact
}

function Write-JackeryBlock {
    param(
        [Parameter(Mandatory)][string] $Title,
        [Parameter(Mandatory)][AllowNull()][object] $Data
    )

    [Console]::Out.WriteLine()
    [Console]::Out.WriteLine("== $Title ==")
    [Console]::Out.WriteLine((ConvertTo-JackeryJson -Data $Data -Compact:($Output -eq 'Json')))
}

function Get-SystemIds {
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]] $Systems)

    $ids = [Collections.Generic.List[string]]::new()
    foreach ($item in $Systems) {
        if ($null -eq $item) { continue }
        $id = Get-JackeryProperty -InputObject $item -Name 'id'
        if ($null -eq $id) {
            $id = Get-JackeryProperty -InputObject $item -Name 'systemId'
        }
        if ($null -ne $id) { $ids.Add([string] $id) }
    }
    return $ids.ToArray()
}

function Test-FollowUpRequested {
    return [bool] (
        $ShowLoginStatus -or $ShowAppUser -or $ListSystems -or $SystemDetail -or
        $ListDevices -or $ShowMonitor -or $ShowDiyDevices -or $ShowMqtt -or
        $DeviceDetail -or $CtDetail -or $AllSystems -or
        -not [string]::IsNullOrWhiteSpace($SystemId) -or
        -not [string]::IsNullOrWhiteSpace($DeviceNo)
    )
}

try {
    if (($DeviceDetail -or $CtDetail) -and [string]::IsNullOrWhiteSpace($DeviceNo)) {
        throw [ArgumentException]::new('-DeviceDetail and -CtDetail require -DeviceNo.')
    }

    $resolvedPassword = Resolve-JackeryPassword
    $script:HttpClient = New-JackeryHttpClient

    $collected = [ordered]@{
        meta = [ordered]@{
            base_url     = $BaseUrl.AbsoluteUri.TrimEnd('/')
            account      = Get-MaskedAccount -Value $Account
            encrypted    = [bool] $Encrypted
            phone_uid    = $PhoneUid
            x_app_version = $script:ClientHeaders['x-app-version']
            user_agent   = $script:ClientHeaders['user-agent']
        }
    }

    $loginData = Connect-JackeryHome -ResolvedPassword $resolvedPassword
    $resolvedPassword = $null
    $Password = $null
    $PlainPassword = $null
    $EncryptedPassword = $null
    $collected['login'] = $loginData
    [Console]::Out.WriteLine('LOGIN OK')

    if ($LoginOnly) {
        Write-JackeryBlock -Title 'LOGIN' -Data $loginData
    }
    else {
        if (-not (Test-FollowUpRequested)) {
            $ShowAppUser = $true
            $ListSystems = $true
            $ListDevices = $true
            $ShowMqtt = $true
        }

        if ($ShowLoginStatus) {
            $collected['login_status'] = Get-JackeryLoginStatus
        }
        if ($ShowAppUser) {
            $collected['app_user'] = Get-JackeryAppUser
        }

        $systems = @()
        if (
            $ListSystems -or $SystemDetail -or $ListDevices -or $ShowMonitor -or
            $ShowDiyDevices -or $AllSystems -or
            -not [string]::IsNullOrWhiteSpace($SystemId)
        ) {
            $systems = @(Get-JackerySystems)
            $collected['systems'] = $systems
        }

        $allSystemIds = @(Get-SystemIds -Systems $systems)
        if (-not [string]::IsNullOrWhiteSpace($SystemId)) {
            $targetSystemIds = @([string] $SystemId)
        }
        elseif ($AllSystems) {
            $targetSystemIds = $allSystemIds
        }
        elseif ($SystemDetail -or $ListDevices -or $ShowMonitor -or $ShowDiyDevices) {
            $targetSystemIds = @($allSystemIds | Select-Object -First 1)
        }
        else {
            $targetSystemIds = @()
        }

        if ($SystemDetail -and $targetSystemIds.Count -gt 0) {
            $collected['system_detail'] = [ordered]@{}
            foreach ($targetId in $targetSystemIds) {
                $collected['system_detail'][$targetId] = Get-JackerySystemDetail -TargetSystemId $targetId
            }
        }
        if ($ShowMonitor -and $targetSystemIds.Count -gt 0) {
            $collected['monitor'] = [ordered]@{}
            foreach ($targetId in $targetSystemIds) {
                $collected['monitor'][$targetId] = Get-JackeryMonitor -TargetSystemId $targetId
            }
        }
        if ($ListDevices -and $targetSystemIds.Count -gt 0) {
            $collected['devices'] = [ordered]@{}
            foreach ($targetId in $targetSystemIds) {
                $collected['devices'][$targetId] = @(Get-JackeryDevices -TargetSystemId $targetId)
            }
        }
        if ($ShowDiyDevices -and $targetSystemIds.Count -gt 0) {
            $collected['diy_devices'] = [ordered]@{}
            foreach ($targetId in $targetSystemIds) {
                $collected['diy_devices'][$targetId] = Get-JackeryDiyDevices -TargetSystemId $targetId
            }
        }
        if ($ShowMqtt) {
            $collected['mqtt'] = Get-JackeryMqttConfig
        }
        if ($DeviceDetail) {
            $collected['device_detail'] = Get-JackeryDeviceDetail -TargetDeviceNo $DeviceNo
        }
        if ($CtDetail) {
            $collected['ct_detail'] = Get-JackeryCtDetail -TargetDeviceNo $DeviceNo
        }

        foreach ($key in @(
            'login_status', 'app_user', 'systems', 'system_detail', 'monitor',
            'devices', 'diy_devices', 'mqtt', 'device_detail', 'ct_detail'
        )) {
            if ($collected.Contains($key)) {
                Write-JackeryBlock -Title $key.ToUpperInvariant() -Data $collected[$key]
            }
        }
    }

    if (-not [string]::IsNullOrWhiteSpace($SavePath)) {
        $fullSavePath = [IO.Path]::GetFullPath($SavePath)
        $savedJson = (ConvertTo-JackeryJson -Data $collected) + [Environment]::NewLine
        [IO.File]::WriteAllText(
            $fullSavePath,
            $savedJson,
            [Text.UTF8Encoding]::new($false)
        )
        Write-Verbose "Saved combined output to '$fullSavePath'."
    }
}
catch [ArgumentException] {
    [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
    exit 1
}
catch {
    [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
    exit 2
}
finally {
    $resolvedPassword = $null
    if ($null -ne $script:HttpClient) {
        $script:HttpClient.Dispose()
    }
}
