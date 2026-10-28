import SwiftUI
import UIKit
import NetworkExtension
import UniformTypeIdentifiers

@main
struct AFZOpenFluxApp: App {
    var body: some Scene { WindowGroup { ConnectionView() } }
}

final class TunnelStore: ObservableObject {
    @Published var document = UserDefaults.standard.string(forKey: "document_url") ?? ""
    @Published var profileStatus = ""
    @Published var status = "Отключено"
    @Published var active = false
    private var manager: NETunnelProviderManager?
    private var observer: NSObjectProtocol?
    private var providerID: String { Bundle.main.bundleIdentifier! + ".Tunnel" }

    init() {
        observer = NotificationCenter.default.addObserver(forName: .NEVPNStatusDidChange, object: nil, queue: .main) { [weak self] _ in self?.refresh() }
        NETunnelProviderManager.loadAllFromPreferences { [weak self] managers, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.manager = managers?.first { ($0.protocolConfiguration as? NETunnelProviderProtocol)?.providerBundleIdentifier == self.providerID }
                if self.document.isEmpty {
                    self.document = (self.manager?.protocolConfiguration as? NETunnelProviderProtocol)?.providerConfiguration?["document_url"] as? String ?? ""
                }
                if error != nil { self.status = "Не удалось прочитать настройки VPN" } else { self.refresh() }
            }
        }
    }
    deinit { if let observer = observer { NotificationCenter.default.removeObserver(observer) } }

    private func refresh() {
        let state = manager?.connection.status ?? .disconnected
        active = state == .connected || state == .connecting || state == .reasserting
        switch state {
        case .connected: status = "Подключено"
        case .connecting, .reasserting: status = "Подключение…"
        case .disconnecting: status = "Отключение…"
        default: status = "Отключено"
        }
    }
    private func valid(_ value: String) -> Bool {
        guard value.count <= 2048, !value.unicodeScalars.contains(where: { $0.value <= 32 }), let u = URLComponents(string: value) else { return false }
        return u.scheme == "https" && u.user == nil && u.password == nil && (u.port == nil || u.port == 443)
            && ["disk.yandex.ru","disk.yandex.com","docs.yandex.ru","docs.yandex.com","disk.360.yandex.ru"].contains(u.host ?? "")
            && !u.path.isEmpty && u.path != "/"
    }
    private func saveProfile(_ value: String) {
        document = value
        UserDefaults.standard.set(value, forKey: "document_url")
        profileStatus = active
            ? "Профиль добавлен. Отключите VPN и подключитесь снова, чтобы применить его."
            : "Профиль добавлен. Нажмите «Подключить»."
    }
    func importLink(_ input: String) {
        let input = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard input.count <= 16384 else { profileStatus = "Неверная ссылка профиля"; return }
        var value = input
        if let url = URLComponents(string: input), url.scheme == "afzopenflux", url.host == "import",
           url.user == nil, url.password == nil, url.port == nil,
           (url.path.isEmpty || url.path == "/"), url.fragment == nil {
            // URLSearchParams uses '+' for spaces; document URLs themselves reject spaces.
            let items = URLComponents(string: input.replacingOccurrences(of: "+", with: "%20"))?.queryItems ?? []
            let versions = items.filter { $0.name == "version" }
            let documents = items.filter { $0.name == "document_url" }
            guard versions.count == 1, versions[0].value == "1", documents.count == 1,
                  let document = documents[0].value else { profileStatus = "Неверная ссылка профиля"; return }
            value = document
        }
        guard valid(value) else { profileStatus = "В боте нажмите «Скопировать настройки» и вставьте ещё раз"; return }
        saveProfile(value)
    }
    func toggle() {
        if active { manager?.connection.stopVPNTunnel(); return }
        let url = document.trimmingCharacters(in: .whitespacesAndNewlines)
        guard valid(url) else { status = "Импортируйте профиль или ссылку из бота"; return }
        let selected = manager ?? NETunnelProviderManager()
        let config = NETunnelProviderProtocol()
        config.providerBundleIdentifier = providerID
        config.serverAddress = "AFZ OpenFlux"
        config.providerConfiguration = ["document_url": url]
        config.disconnectOnSleep = false
        selected.protocolConfiguration = config
        selected.localizedDescription = "AFZ OpenFlux"
        selected.isEnabled = true
        manager = selected
        status = "Сохранение настроек…"
        selected.saveToPreferences { [weak self] error in
            if let error = error { DispatchQueue.main.async { self?.status = error.localizedDescription }; return }
            selected.loadFromPreferences { error in
                DispatchQueue.main.async {
                    if let error = error { self?.status = error.localizedDescription; return }
                    do { try selected.connection.startVPNTunnel(); self?.refresh() }
                    catch { self?.status = error.localizedDescription }
                }
            }
        }
    }
    func importProfile(_ result: Result<URL, Error>) {
        do {
            let url = try result.get()
            let access = url.startAccessingSecurityScopedResource()
            defer { if access { url.stopAccessingSecurityScopedResource() } }
            let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard size <= 65536 else { throw CocoaError(.fileReadTooLarge) }
            let data = try Data(contentsOf: url)
            guard data.count <= 65536,
                  let profile = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  profile["version"] as? Int == 1,
                  let value = profile["document_url"] as? String, valid(value) else { throw CocoaError(.fileReadCorruptFile) }
            saveProfile(value)
        } catch { status = "Не удалось прочитать профиль из бота" }
    }
}

struct ConnectionView: View {
    @StateObject private var store = TunnelStore()
    @State private var importing = false
    var body: some View {
        NavigationView {
            Form {
                Section {
                    Label("AFZ OpenFlux", systemImage: "bolt.shield.fill").font(.largeTitle).foregroundColor(.green)
                    Text("Тестовое подключение через Яндекс")
                    Text(store.status).font(.headline)
                }
                Section("Профиль подключения") {
                    if !store.profileStatus.isEmpty { Text(store.profileStatus) }
                    Button("Вставить из буфера") { store.importLink(UIPasteboard.general.string ?? "") }
                    Button("Импортировать файл из бота") { importing = true }
                    TextField("Ссылка на документ из бота", text: $store.document)
                        .textInputAutocapitalization(.never).disableAutocorrection(true).keyboardType(.URL)
                    Button(store.active ? "Отключить" : "Подключить") { store.toggle() }
                        .font(.headline)
                }
                Section {
                    Link("Проверить IP в Safari", destination: URL(string: "https://api.ipify.org/")!)
                    Text("Подключение заменит другой активный VPN на телефоне. Для возврата остановите OpenFlux и включите привычный VPN.")
                    Text("Тестовый режим передаёт TCP/IPv4. Используйте HTTPS. Звонки, игры и приложения, требующие UDP, могут не работать. Обход белых списков зависит от сети оператора.")
                    Text("Один профиль — одно тестовое устройство одновременно.")
                }.font(.footnote)
            }
            .navigationBarHidden(true)
            .fileImporter(isPresented: $importing, allowedContentTypes: [.json], onCompletion: store.importProfile)
            .onOpenURL { store.importLink($0.absoluteString) }
        }.navigationViewStyle(.stack)
    }
}
