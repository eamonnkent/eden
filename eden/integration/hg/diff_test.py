    def check_output(self, output, expected_lines):
        self.assertListEqual(output_lines, expected_lines)
            'diff --git a/dir1/a.txt b/dir1/a.txt',
            '--- a/dir1/a.txt',
            '+++ b/dir1/a.txt',
            'diff --git a/dir1/b.txt b/dir1/b.txt',
            'new file mode 100644',
            '--- /dev/null',
            '+++ b/dir1/b.txt',