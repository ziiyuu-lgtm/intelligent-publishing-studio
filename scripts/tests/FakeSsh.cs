// Disposable Windows test fixture. Never opens a network connection.
using System;
using System.IO;
using System.Text;

public static class FakeSsh
{
    public static int Main(string[] args)
    {
        if (Environment.GetEnvironmentVariable("ALTAVIEN_FAKE_SSH") != "1")
            return 125;
        string trace = Environment.GetEnvironmentVariable("ALTAVIEN_FAKE_SSH_TRACE");
        string[] encoded = Array.ConvertAll(args,
            value => Convert.ToBase64String(Encoding.UTF8.GetBytes(value)));
        File.WriteAllLines(trace, encoded);
        int code = Int32.Parse(Environment.GetEnvironmentVariable("ALTAVIEN_FAKE_SSH_EXIT"));
        Console.WriteLine("FICTIONAL_SSH_EXIT=" + code);
        return code;
    }
}
