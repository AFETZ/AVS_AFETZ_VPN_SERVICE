import uk.superbebra.afzopenflux.ImportLink;
import java.net.URLEncoder;

public final class ImportLinkTest {
    public static void main(String[] args) throws Exception {
        String doc = "https://disk.yandex.ru/i/test?a=1&b=%2B+#part";
        String query = "version=1&document_url=" + URLEncoder.encode(doc, "UTF-8");
        for (String value : new String[]{doc, "  " + doc + "  ", "afzopenflux://import?" + query,
                "https://link.superbebra.uk/connection?client=openflux#" + query}) {
            if (!doc.equals(ImportLink.parse(value))) throw new AssertionError("Profile changed");
        }
        for (String invalid : new String[]{"", "javascript:alert(1)", "https://disk.yandex.ru.evil.test/i/x",
                "https://user@disk.yandex.ru/i/x", "http://disk.yandex.ru/i/x", "https://disk.yandex.ru/",
                "afzopenflux://import?" + query + "&version=1", "afzopenflux://import?" + query + "&document_url=x",
                "afzopenflux://import?version=2&document_url=" + URLEncoder.encode(doc,"UTF-8"),
                "afzopenflux://other?" + query, "afzopenflux://import?version=1&document_url=%GG",
                "https://evil.test/connection?client=openflux#" + query,
                "https://disk.yandex.ru\\@evil.test/i/x", "https://disk.yandex.ru/i/test\n--debug"}) {
            try {ImportLink.parse(invalid); throw new AssertionError("Invalid import accepted");}
            catch (IllegalArgumentException expected) {}
        }
        System.out.println("Native import parser: 4 valid and 14 invalid inputs passed");
    }
}
