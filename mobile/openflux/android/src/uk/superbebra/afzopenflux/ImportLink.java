package uk.superbebra.afzopenflux;

import java.net.URI;
import java.net.URLDecoder;
import java.util.HashMap;
import java.util.Map;
import java.util.Arrays;

/** Parse locally: importing a link must never fetch an arbitrary URL. */
public final class ImportLink {
    private ImportLink() {}

    public static String parse(String input) {
        try {
            if (input == null || input.length() > 16384) throw new IllegalArgumentException();
            String value = input.trim();
            URI uri = new URI(value);
            if ("afzopenflux".equals(uri.getScheme()) && "import".equals(uri.getHost())
                    && uri.getRawUserInfo() == null && uri.getPort() == -1
                    && (uri.getRawPath().isEmpty() || "/".equals(uri.getRawPath()))
                    && uri.getRawFragment() == null) {
                value = profile(uri.getRawQuery());
            } else if ("https".equals(uri.getScheme())
                    && Arrays.asList("link.superbebra.uk", "afzvpn.superbebra.uk", "yk.superbebra.uk").contains(uri.getHost())
                    && uri.getRawUserInfo() == null && (uri.getPort() == -1 || uri.getPort() == 443)
                    && "/connection".equals(uri.getPath())
                    && "openflux".equals(params(uri.getRawQuery()).get("client"))) {
                value = profile(uri.getRawFragment());
            }
            URI document = new URI(value);
            if (value.length() > 2048 || !"https".equals(document.getScheme())
                    || document.getRawUserInfo() != null || (document.getPort() != -1 && document.getPort() != 443)
                    || !Arrays.asList("disk.yandex.ru", "disk.yandex.com", "docs.yandex.ru", "docs.yandex.com", "disk.360.yandex.ru").contains(document.getHost())
                    || document.getRawPath().isEmpty() || "/".equals(document.getRawPath())) throw new IllegalArgumentException();
            for (int i = 0; i < value.length(); i++) if (value.charAt(i) <= 32) throw new IllegalArgumentException();
            return value;
        } catch (Exception error) {
            throw new IllegalArgumentException("Invalid AFZ OpenFlux profile");
        }
    }

    private static String profile(String query) throws Exception {
        Map<String, String> values = params(query);
        if (!"1".equals(values.get("version")) || !values.containsKey("document_url")) throw new IllegalArgumentException();
        return values.get("document_url");
    }

    private static Map<String, String> params(String query) throws Exception {
        Map<String, String> values = new HashMap<>();
        if (query == null) throw new IllegalArgumentException();
        for (String part : query.split("&")) {
            String[] pair = part.split("=", 2);
            if (pair.length != 2) throw new IllegalArgumentException();
            String key = URLDecoder.decode(pair[0], "UTF-8");
            if (values.put(key, URLDecoder.decode(pair[1], "UTF-8")) != null) throw new IllegalArgumentException();
        }
        return values;
    }
}
